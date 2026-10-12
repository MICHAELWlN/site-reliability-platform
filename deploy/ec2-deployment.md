# EC2 deployment

Jenkins publishes an immutable `linux/amd64` image to the private ECR repository `sre-platform`, then the `Jenkinsfile` stage **Deploy to EC2** sends that verified digest to the registered SSM document `SREPlatformDeploy` version 1. Build #5 of temporary job `sre-platform-ci-ecr-test` completed that stage.

```text
Jenkins → Docker → ECR → SSM document SREPlatformDeploy → EC2 → Nginx
```

Three results are separate:

- **Successful automated deployment.** Build #5 published and deployed `sha256:6951edc16cb827ad3c13814eaa9fb490ffb9f1402453e7ccad31de376ac48e8e`. SSM command `a4a8d4f3-36a7-418b-80c1-b9c8382a554e` finished Success with exit 0. Nginx switched to `127.0.0.1:18001`.
- **Automatic deployment rollback.** `deploy/ec2_deploy.py` can restore the previous Nginx upstream and container when a cutover fails. `deploy/test_ec2_deploy.py` covers that against a fake host. Build #5 was a successful cutover, so this path has not been exercised on the instance.
- **Automated failure detection and manual runtime recovery.** A historical outage stopped `sre-service` by hand and started it again by hand while Nginx still proxied to that process. The on-host monitor, CloudWatch, and SNS observed that outage and the recovery. While the instance was running, public traffic went through Nginx to the container on `127.0.0.1:18001`. Controlled manual failure injection on that path produced failed Datadog HTTP checks. After multiple failed checks the Datadog monitor went from OK to ALERT. Manual application recovery followed, and the monitor returned from ALERT to OK. The exact injection command is not recorded. Neither demonstration used the deploy script's automatic rollback. The instance is now stopped, and the Datadog synthetic test is paused.

What is installed:

- `/usr/local/sbin/sre-platform-deploy` is installed from `deploy/ec2_deploy.py`. Build #5 invoked it through the document.
- `SREPlatformDeploy` version 1 is registered and Active. Its command is the installed script, and its only parameter is `imageDigest`.
- `deploy/sre-monitor.service` was installed, active, and enabled during the demonstration. It checks `http://127.0.0.1/health` and publishes `Availability`, `LatencyMs`, and `Failures` to CloudWatch namespace `SREPlatform`. The instance is now stopped.

An earlier invocation of the same document, outside Jenkins, targeted the digest that was already serving traffic. That run was a no-op apart from setting the container restart policy to `unless-stopped`. `sre-service` stayed active. Build #5 is the cutover that followed.

## What Build #5 left running

These facts were verified while the instance was running. The instance is now stopped.

- Instance `i-0de56d01b124c0e69` in `us-east-2`.
- systemd unit `sre-service` listened on `127.0.0.1:8000`. Nginx did not proxy to it.
- Container `sre-platform-6951edc16cb8` served digest `sha256:6951edc16cb827ad3c13814eaa9fb490ffb9f1402453e7ccad31de376ac48e8e` on `127.0.0.1:18001`. Its restart policy is `unless-stopped`. It passed the container health check, and `/health` through Nginx returned HTTP 200.
- Container `sre-platform-candidate` is stopped and retained. It had been serving `sha256:5f6ab0e1caf0bc49ff1b9cb6cc14885a10b60847b8e4096405b00d7eee70755d` on `127.0.0.1:18000`. The invocation logged `retire_previous` with action `stop`.
- Nginx proxied port 80 to `127.0.0.1:18001`. The cutover wrote a backup of the previous site file under `/var/backups`.
- `deploy/nginx.conf` in git is still the original file, with `proxy_pass` pointed at port 8000. The script does not copy that file onto the instance. It edits the live file `/etc/nginx/sites-available/sre-platform`.
- The source snapshot for Build #5 is `1654be9a8e0e08ce4c480b01f18b41b62da88ed4`. It lives in the Jenkins test repository and is not a commit in this project.

## Deployment strategy

`deploy/ec2_deploy.py` accepts one argument, `--digest`. The digest must be `sha256:` and 64 lowercase hex characters. The repository and region are fixed: `sre-platform` in `us-east-2`. The registry host is built from the account returned by the instance role. The account is not stored in git.

On the instance the script takes an exclusive `flock` on `/var/lock/sre-platform-deploy.lock` and releases it on every path, including failure. A second run exits 11 without changing containers or Nginx.

1. It records the digest of the container Nginx is already serving.
2. If that container is healthy and already runs this digest, it sets `--restart unless-stopped` and exits 0. It does not pull or create another container.
3. Otherwise it checks that at least 180 MiB of memory is free while the previous container is still running, and that at least 500 MiB of disk is free. It checks memory again immediately before starting the candidate.
4. It logs in to ECR with the instance role, using a mode `0700` temporary Docker config, pulls that digest, and deletes the config on every path.
5. It refuses the image unless Docker reports `linux/amd64` and user `appuser`.
6. It starts a new container with `--restart no`, published only as `127.0.0.1:<port>:8000`. The port is the first free port from 18000 through 18019, and it is not the port Nginx is already using.
7. It waits up to 60 seconds for Docker's health check and for HTTP 200 from `http://127.0.0.1:<port>/health` with a healthy status body.
8. It copies the Nginx site to `/var/backups`, changes only the `proxy_pass` line, runs `nginx -t`, and reloads Nginx. It does not restart Nginx.
9. It requires `nginx -T` to show that upstream and HTTP 200 from `http://127.0.0.1/health`.
10. It then sets the new container to `--restart unless-stopped` and stops the previous platform container. That stopped container and its image are kept. Older stopped platform containers, and `sre-platform` images no remaining container uses, are removed. Other Docker images and containers are not pruned.

Every path writes one JSON object per line. The log includes the digest, port, container name, and result. It does not include registry passwords, tokens, or the account id.

Failures exit non-zero:

| Code | Meaning |
| --- | --- |
| 2 | Digest is not an immutable sha256 value |
| 3 | Not enough free memory or disk to start another container |
| 4 | ECR login or image pull failed |
| 5 | Image is not `linux/amd64` or not `appuser` |
| 6 | Candidate did not become healthy |
| 7 | `nginx -t` failed |
| 8 | Nginx reload failed |
| 9 | Nginx came up but the health check through it failed |
| 10 | The new container is serving, but restart policy, stopping the previous container, or ending with one running platform container failed |
| 11 | Another deployment already holds the lock |
| 12 | Cutover failed and rollback could not restore the previous endpoint |

## Container lifecycle

The candidate starts with restart policy `no`, so a container that fails its health check does not restart in a loop. After Nginx is serving it, `docker update --restart unless-stopped` keeps it across a reboot without a new systemd unit. A failed candidate is removed and is never given that policy.

The previous container stays running until the health check through Nginx passes. Only then is it set back to restart policy `no` and stopped. Its digest is left on that stopped container. A repeated deploy of the active digest does not create a second container and does not delete that rollback container. The script never stops `sre-service`.

## Rollback

| Failure | What is left in place |
| --- | --- |
| Pull, architecture, or user check | Current container and Nginx are unchanged. The temporary Docker config is removed. |
| Candidate health check | The new container is removed. Nginx is unchanged. |
| `nginx -t` | The site file is restored. Nginx is left on the config it already loaded. The previous container is checked, and the new container is removed. |
| Reload or the check through Nginx | The site file is restored, `nginx -t` runs, and Nginx is reloaded. The previous container is started again if it is stopped. Its digest is compared with the digest recorded at the start. HTTP 200 is required from that container and from Nginx. The new container is removed. |
| Rollback cannot restore that health | Exit 12. The previous digest is still the one recorded on the previous container when that container remains. |
| Restart policy after a good switch | Nginx stays on the new container. A second run of the same digest only retries the restart policy. |

Manual rollback to the systemd application is still the earlier procedure: point `proxy_pass` back at `127.0.0.1:8000`, run `nginx -t`, and reload Nginx. The script does not stop `sre-service`. The instance is now stopped.

Manual rollback to the previous container, after a successful deploy has stopped it: `docker start` that container, point `proxy_pass` at its localhost port, test, and reload.

## SSM document

`deploy/ssm/SREPlatformDeploy.json` is the Command document registered as `SREPlatformDeploy` version 1. It has one parameter, `imageDigest`. The allowed pattern is `^sha256:[0-9a-f]{64}$`: the prefix `sha256:`, then exactly 64 lowercase hex characters. Uppercase hex, a different length, spaces, quotes, and shell metacharacters do not match. The script checks the same pattern again before it takes the lock. Its only command is:

```text
/usr/local/sbin/sre-platform-deploy --digest '{{ imageDigest }}'
```

The `aws:runShellScript` action is the Systems Manager plugin that runs that fixed command. It is not permission to use the shared `AWS-RunShellScript` document, and the document has no parameter for a shell command. Jenkins calls document version 1 by name.

The `description` field in `deploy/ssm/SREPlatformDeploy.json` no longer says the file is unregistered. The registered AWS document was not updated, so AWS can still show the previous description until that document is replaced.

## Tests

`deploy/test_ec2_deploy.py` runs the deployment decisions against a fake host. It does not call AWS, Docker, or Nginx. It covers a successful switch, a failed pull, a failed container health check, a failed `nginx -t`, rollback after a failed reload, rollback after a failed health check through Nginx, a rollback that cannot restore health, a second deploy rejected while the lock is held, preservation of the previous container and digest, restart policy, and two runs of the same digest.

```text
python3 deploy/test_ec2_deploy.py
python3 jenkins/test_ssm_deploy_tool.py
```

`jenkins/test_ssm_deploy_tool.py` checks the Jenkins decision logic without calling AWS: a successful invocation, a failed invocation, a timeout, a bad digest, missing credentials, and a repeated noop.

## Known limitations

- Build #5 ran the Jenkins stage and completed a successful cutover. Automatic rollback has not been exercised on the instance.
- The Jenkins deploy role can still run `AWS-RunShellScript` on this instance. That was used to copy the script. The pipeline stage does not call that document. Until the permission is removed, the new document is not the only command the role can send.
- The instance role's ability to pull from ECR was already verified. No new instance-role permission was added. The policy document on the Jenkins role was not readable from the lab identity, so this file does not claim to have reviewed it.
- `sre-service` still occupies port 8000 and memory. The script will not stop it.
- A second container needs the free memory checked above. The last manual check saw about 406 MiB available and about 2.8 GB of free disk.
- While the instance was running, the serving container used restart policy `unless-stopped`, and `sre-service` was still running on port 8000. The instance is now stopped.
- Both containers answer `/health` with the same body. The script treats `nginx -T` plus HTTP 200 as the switch check. It does not prove which build is behind Nginx by reading a version header.
- `deploy/sre-monitor.service` targets `http://127.0.0.1/health`, so CloudWatch follows Nginx. `monitor/monitor.py` still defaults to `127.0.0.1:8000` when that flag is omitted.
- A successful deploy keeps the container it just replaced and removes older stopped platform containers. It removes unused `sre-platform` images only. It does not prune unrelated Docker resources.

## Remaining steps

1. Remove the Jenkins role's permission to run `AWS-RunShellScript`, leaving `ssm:SendCommand` and `ssm:GetCommandInvocation` for `SREPlatformDeploy` on `i-0de56d01b124c0e69`.
2. Exercise the deploy script's automatic rollback on the instance. Build #5 did not do that.
3. Decide later whether to stop `sre-service` after the container has survived a reboot.
