# Terraform: private ECR repository (deployment, phase 1)

This configuration provisions **one private Amazon ECR repository** named
`sre-platform`, plus a lifecycle policy for it. It is the first building block for
a future automated deployment path.

It is separate from `terraform/demo/` and has its own state, so the two cannot affect
each other.

## Status

- **Planned only.** `terraform plan` has been reviewed. `terraform apply` has not been
  run, so the repository does not exist yet.
- **Jenkins deployment integration is not implemented.** The `Jenkinsfile` still
  builds and tests the image locally and does not push to ECR or deploy to AWS.
- **EC2 is unchanged.** The existing instance still runs the application from the
  hand-built systemd and Nginx setup. Nothing in this directory touches it.

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

## Intended future flow (not implemented)

### Jenkins pushes images

A later phase would extend the pipeline so that, after the tests and the local
health check pass, Jenkins would:

1. Authenticate to ECR with a short-lived token from `aws ecr get-login-password`.
2. Tag the image `<repository_url>:<unique tag>`.
3. Push it.

This needs a dedicated, least-privilege identity for Jenkins that is allowed
`ecr:GetAuthorizationToken` and the push actions on this repository only. That
identity, and how Jenkins would hold its credentials, have not been designed or
created. Tags must be unique because the repository is immutable. The current
pipeline tag, `ci-${BUILD_NUMBER}`, restarts from 1 if Jenkins is rebuilt, so a Git
commit SHA would be a safer tag for pushes.

### EC2 pulls images

The EC2 instance role already includes `AmazonEC2ContainerRegistryReadOnly`, which
permits pulling from ECR in the account without any stored keys. A later phase would
have the instance:

1. Log in with the instance role: `aws ecr get-login-password --region us-east-2`
   piped to `docker login` for the registry host in `repository_url`.
2. Pull the chosen tag.
3. Run it.

The instance would also need Docker installed, which has not been verified. This
describes the intended mechanism. Nothing has been run against the instance.

## Limitations

- Local Terraform state only; there is no remote backend or locking.
- Basic scan-on-push only. Scanning does not block pushes or deployments.
- The lifecycle policy counts all images, so a rollback target older than the newest
  10 would be expired.
- AWS-managed encryption was chosen over a customer-managed KMS key for simplicity.
- Nothing here has been applied, so none of the AWS-side behaviour above has been
  observed yet.
