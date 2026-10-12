variable "aws_region" {
  description = "AWS region for the ECR repository."
  type        = string
  default     = "us-east-2"
}

variable "aws_profile" {
  description = "Named AWS CLI profile used for credentials. No keys are stored in this configuration."
  type        = string
  default     = "sre-lab"
}

variable "repository_name" {
  description = "Name of the private ECR repository."
  type        = string
  default     = "sre-platform"
}

variable "retained_image_count" {
  description = "Number of most recent images the lifecycle policy keeps. Older images expire."
  type        = number
  default     = 10

  validation {
    condition     = var.retained_image_count >= 1 && floor(var.retained_image_count) == var.retained_image_count
    error_message = "retained_image_count must be a whole number of at least 1."
  }
}
