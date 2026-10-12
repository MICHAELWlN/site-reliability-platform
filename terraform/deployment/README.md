# Terraform: private ECR repository (deployment, phase 1)

This configuration provisions **one private Amazon ECR repository** named
`sre-platform`, plus a lifecycle policy for it. It is the first building block for
the repository Jenkins publishes to. Deployment to EC2 is not defined in this directory.

It is separate from `terraform/demo/` and has its own state, so the two cannot affect
each other.

## Status

- **Repository exists.** It is the private repository Jenkins publishes to. This directory
  was not changed by the Jenkins integration work.
- **Jenkins publishes images here.** After tests and a local health check, the pipeline
  pushes a `linux/amd64` image tagged with the full commit SHA. Build #5 then deployed
  `sha256:6951edc16cb827ad3c13814eaa9fb490ffb9f1402453e7ccad31de376ac48e8e`. Nginx serves it
  on `127.0.0.1:18001`. See `jenkins/README.md`. This directory was not part of that deploy.
- **This directory does not manage EC2.** Nothing here changes the instance, its
  security groups, Nginx, or systemd.

## What it creates

| Resource | Purpose |
|---|---|
| `aws_ecr_repository.platform` | Private repository `sre-platform` |
| `aws_ecr_lifecycle_policy.platform` | Keeps the 10 most recent images and expires older ones |

Repository settings:

- Private ECR repository. No repository policy is attached, so access is controlled
  only by IAM in the owning account.
- Image tag mutability: `IMMUTABLE`. A tag, once pushed, cannot be overwritten.
- Scan on push: enabled (basic scanning through `scan_on_push`).
- Encryption: `AES256` (AWS-managed).
- Tags: `Project = site-reliability-platform`, `Environment = lab`, `ManagedBy = Terraform`.

Lifecycle policy: one rule, priority 1, matching all images (`tagStatus = any`),
expiring any image beyond the newest 10 (`imageCountMoreThan`). It removes old images
only. Terraform destroy is the only way the repository itself is removed.

## Files

| File | Purpose |
|---|---|
| `versions.tf` | Terraform `>= 1.5.0`, provider `hashicorp/aws ~> 6.0` |
| `providers.tf` | AWS provider using the named profile `sre-lab`, region `us-east-2` |
| `variables.tf` | Region, profile, repository name, retained image count |
| `main.tf` | The repository and the lifecycle policy |
| `outputs.tf` | Repository URL, ARN and name |
| `terraform.tfvars.example` | Placeholder for optional overrides (all values are defaults) |
| `.terraform.lock.hcl` | Provider version lock, committed |

No credentials are stored in this configuration. It uses the `sre-lab` named profile
from your local AWS CLI configuration. `terraform.tfvars`, state files and saved plans
are excluded by the root `.gitignore`.

## Usage

```sh
cd terraform/deployment
terraform fmt -check
terraform init
terraform validate
AWS_PROFILE=sre-lab terraform plan -out=tfplan-ecr
terraform show tfplan-ecr          # review before anything else
```

Applying is a separate, deliberate step and is not part of phase 1:

```sh
AWS_PROFILE=sre-lab terraform apply tfplan-ecr
```

### Outputs

After an apply, `terraform output` shows `repository_url`, `repository_arn` and
`repository_name`.

## Required IAM permissions

The plan creates new resources and does not call ECR, so it succeeds without any ECR
permission. An apply (and any later `plan` that refreshes existing state) needs ECR
permissions for the identity that runs Terraform. At the time of the phase 1 plan,
the `sre-lab` identity was **not** authorized for `ecr:DescribeRepositories`, so
expect to grant the following before applying (this configuration does not grant them):

- `ecr:CreateRepository`
- `ecr:DescribeRepositories`
- `ecr:ListTagsForResource`
- `ecr:TagResource`
- `ecr:PutLifecyclePolicy`
- `ecr:GetLifecyclePolicy`
- `ecr:PutImageScanningConfiguration`
- `ecr:PutImageTagMutability`
- `ecr:DeleteRepository` and `ecr:DeleteLifecyclePolicy` (only for a later destroy)

This list is the expected set for these two resources, not a result that has been
tested. Confirm it against the real apply.

## How images get here

### Jenkins pushes images (verified)

After the unit tests and the local health check pass, the pipeline in `../Jenkinsfile`:

1. Checks that the caller is `assumed-role/sre-jenkins-deploy-role`.
2. Authenticates with `aws ecr get-login-password` piped to `docker login --password-stdin`.
3. Pushes `linux/amd64` as `<repository_url>:<full commit SHA>` when that tag is absent.

Tags are immutable. A later build of the same commit reuses the image only after the digest matches.
The role session is short-lived and is removed when the build ends. Build #4 of the temporary Jenkins
job published one image this way. Build #5 published
`sha256:6951edc16cb827ad3c13814eaa9fb490ffb9f1402453e7ccad31de376ac48e8e`. See `jenkins/README.md`.

### EC2 pulls images (verified on Build #5)

The EC2 instance role already includes `AmazonEC2ContainerRegistryReadOnly`, which
permits pulling from ECR in the account without any stored keys. During Build #5 the installed
host script, not Terraform, did this on the instance:

1. Log in with the instance role: `aws ecr get-login-password --region us-east-2`
   piped to `docker login` for the registry host derived from the instance role.
2. Pull the verified digest, not a mutable tag.
3. Run it on `127.0.0.1:18001` and switch Nginx after the health checks passed.

Docker is installed on the instance. That was shown by the successful pull and the running container.
This Terraform directory still does not manage the instance, Nginx, or systemd.

## Limitations

- Local Terraform state only; there is no remote backend or locking.
- Basic scan-on-push only. Scanning does not block pushes or deployments.
- The lifecycle policy counts all images, so a rollback target older than the newest
  10 would be expired.
- AWS-managed encryption was chosen over a customer-managed KMS key for simplicity.
- The repository exists and Jenkins publishes to it. Expiry of images beyond the newest
  10 has not been observed. This directory does not deploy containers.
