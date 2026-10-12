locals {
  tags = {
    Project     = "site-reliability-platform"
    Environment = "lab"
    ManagedBy   = "Terraform"
  }
}

# Standard private ECR repository. Access is controlled by IAM only; no
# repository policy is attached, so nothing is shared publicly or cross-account.
resource "aws_ecr_repository" "platform" {
  name                 = var.repository_name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256" # AWS-managed encryption
  }

  tags = local.tags
}

# Keep the most recent images; expire older ones. This removes old images only
# and never deletes the repository itself.
resource "aws_ecr_lifecycle_policy" "platform" {
  repository = aws_ecr_repository.platform.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Retain the ${var.retained_image_count} most recent images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.retained_image_count
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
