locals {
  tags = {
    Project     = "site-reliability-platform"
    Environment = "demo"
    ManagedBy   = "Terraform"
  }
}

# One isolated security group. Nothing references or attaches it.
# ingress and egress are set to empty lists so Terraform owns the rule set:
# no inbound rules, and no outbound rules (the AWS default allow-all egress rule
# is not kept). Any rule added outside Terraform would show up as drift.
resource "aws_security_group" "demo" {
  name        = "sre-terraform-demo"
  description = "Isolated Terraform demonstration"
  vpc_id      = var.vpc_id

  ingress = []
  egress  = []

  tags = local.tags
}
