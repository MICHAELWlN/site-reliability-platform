# Terraform Demo: Isolated Security Group

A minimal Infrastructure as Code demonstration for the Site Reliability Platform.
It defines **one** AWS resource and does not touch the existing EC2 instance, Nginx, systemd,
CloudWatch, SNS, or any existing security group.

**Status:** the security group was created, verified, and then destroyed. It no longer exists in AWS or in
Terraform state. The configuration remains so the demo can be repeated.

> Real VPC and security-group IDs are replaced with placeholders in this document. `<VPC_ID>` is the existing
> VPC you supply through `vpc_id`, and `<SECURITY_GROUP_ID>` is the ID Terraform prints for the group it creates.

## Resources implemented

| Terraform address | AWS resource | Key settings |
| --- | --- | --- |
| `aws_security_group.demo` | Security group | name `sre-terraform-demo`, description `Isolated Terraform demonstration` |

- **Region:** `us-east-2`
- **VPC:** `<VPC_ID>` (an existing VPC, supplied through the `vpc_id` variable)
- **AWS profile:** `sre-lab` (named CLI profile; no keys are stored in this configuration)
- **Rules:** none. `ingress = []` and `egress = []` are set explicitly, so the group has no inbound
  rules, no outbound rules, and no public ingress. Any rule added outside Terraform shows up as drift.
- **Tags:** `Project = site-reliability-platform`, `Environment = demo`, `ManagedBy = Terraform`
- **Attachments:** none. Nothing references this group.
- **Outputs:** `security_group_id`, `security_group_name`
- **Providers:** Terraform `>= 1.5.0`, `hashicorp/aws ~> 6.0` (locked to `6.68.0` in `.terraform.lock.hcl`)

| File | Purpose |
| --- | --- |
| `versions.tf` | Terraform and provider version constraints |
| `providers.tf` | AWS provider using the region and profile variables |
| `variables.tf` | `aws_region`, `aws_profile`, `vpc_id` (validated) |
| `main.tf` | The security group and tags |
| `outputs.tf` | Security group ID and name |
| `terraform.tfvars.example` | Template for your local, Git-ignored `terraform.tfvars` |
| `.terraform.lock.hcl` | Provider version lock. Committed on purpose |

## Terraform lifecycle commands

Run from `terraform/demo/`.

```bash
# One-time: create your local variable file (Git-ignored) and set vpc_id
cp terraform.tfvars.example terraform.tfvars

export AWS_PROFILE=sre-lab
aws sts get-caller-identity --query Arn --output text   # confirm it is not root

terraform fmt -check        # formatting
terraform init              # download the provider, honoring the lock file
terraform validate          # static validation, no AWS calls
terraform plan -out=tfplan-fresh
terraform show tfplan-fresh # review: expect 1 to add, 0 to change, 0 to destroy
terraform apply tfplan-fresh
terraform plan -detailed-exitcode   # drift check: exit 0 means no differences
```

Apply the saved plan file, not a fresh `apply`, so exactly what you reviewed is what runs.
Plan files are Git-ignored (`tfplan*`).

## Verified results

### Apply

```text
aws_security_group.demo: Creation complete after 1s [id=<SECURITY_GROUP_ID>]
Apply complete! Resources: 1 added, 0 changed, 0 destroyed.

security_group_id   = "<SECURITY_GROUP_ID>"
security_group_name = "sre-terraform-demo"
```

Before applying, the plan was checked programmatically: exactly one create (`aws_security_group.demo`),
0 changes, 0 destroys, the expected VPC and region, and 0 inbound and 0 outbound rules.

The first apply attempt failed with `UnauthorizedOperation` on `ec2:CreateSecurityGroup`, because the IAM
user's policy did not yet allow it. Nothing was created. After the policy was updated, a freshly generated
plan was applied successfully. The failure is a useful record that the credentials are scoped, not admin.

### AWS verification

Read back with `aws ec2 describe-security-groups --group-ids <SECURITY_GROUP_ID>`:

- Name `sre-terraform-demo`, description `Isolated Terraform demonstration`
- VPC `<VPC_ID>`
- Inbound rules: 0. Outbound rules: 0
- Tags: `Project=site-reliability-platform`, `Environment=demo`, `ManagedBy=Terraform` (exact match)
- The existing security groups in the VPC (`sre-platform-sg`, `default`) had identical contents before and after the apply.

### Drift check

`terraform plan -detailed-exitcode` exited `0`:
"Terraform has compared your real infrastructure against your configuration and found no differences."

### Destroy and cleanup

The demo resource was then destroyed with Terraform:

- `terraform destroy` completed successfully.
- `terraform state list` returned empty: Terraform no longer manages any resources.
- Querying the security group in AWS returned `InvalidGroup.NotFound`, confirming the group no longer exists.

The successful destroy also confirms the IAM policy allowed `ec2:DeleteSecurityGroup`. These results were
recorded by the project owner. The destroy plan output and any screenshots are not committed.

## Destroying the demo resource

The recommended safe sequence for destroying the demo resource if you apply it again. Destroying removes
only `aws_security_group.demo`. Do not use `-auto-approve`.

```bash
cd terraform/demo
export AWS_PROFILE=sre-lab
aws sts get-caller-identity --query Arn --output text    # confirm identity, not root

# 1. Confirm nothing is using the group (expect empty output)
aws ec2 describe-network-interfaces --region us-east-2 \
  --filters Name=group-id,Values=<SECURITY_GROUP_ID> \
  --query 'NetworkInterfaces[].NetworkInterfaceId' --output text

# 2. Plan the destroy and review it
terraform plan -destroy -out=tfplan-destroy
terraform show tfplan-destroy    # expect: 1 to destroy, only aws_security_group.demo

# 3. Apply exactly that plan
terraform apply tfplan-destroy

# 4. Confirm it is gone (expect 0)
aws ec2 describe-security-groups --region us-east-2 \
  --filters Name=group-name,Values=sre-terraform-demo --query 'length(SecurityGroups)' --output text
```

Destroy needs `ec2:DeleteSecurityGroup`, which the completed destroy exercised successfully.
A security group that is attached to a network interface cannot be deleted. Do not attach this demo group
to the real EC2 instance.

## Known limitations

- **Local state.** State lives in `terraform.tfstate` on one machine. It is Git-ignored, but there is no
  remote backend, no locking, no versioning, and no backup beyond `terraform.tfstate.backup`.
  Losing the file orphans the resource. A production setup would use an S3 backend with locking.
- **Narrow scope.** Only one unattached security group was defined, and it has since been destroyed. The EC2 instance, existing security
  group, Nginx, systemd units, CloudWatch, and SNS were created by hand and are **not** managed or imported
  by Terraform.
- **No automation.** Terraform is not part of the Jenkins pipeline. `fmt`, `validate`, and `plan` are
  run manually.
- **Local variable file.** `vpc_id` has no default. Each machine needs its own `terraform.tfvars`.
- **Default VPC.** The target VPC is the account's default VPC.
- **Scoped IAM.** The `sre-lab` user's policy is deliberately limited, and some read calls (for example
  `ec2:DescribeInstances` and `ec2:DescribeSecurityGroupRules`) are denied.
- **Provider drift over time.** `~> 6.0` allows newer 6.x releases when the lock file is upgraded.
