variable "aws_region" {
  description = "AWS region for the demonstration."
  type        = string
  default     = "us-east-2"
}

variable "aws_profile" {
  description = "Named AWS CLI profile used for credentials. No keys are stored in this configuration."
  type        = string
  default     = "sre-lab"
}

variable "vpc_id" {
  description = "Existing VPC that will contain the demonstration security group."
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]{8,17}$", var.vpc_id))
    error_message = "vpc_id must look like vpc-0123456789abcdef0."
  }
}
