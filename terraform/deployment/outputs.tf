output "repository_url" {
  description = "ECR repository URL used for docker tag, push and pull."
  value       = aws_ecr_repository.platform.repository_url
}

output "repository_arn" {
  description = "ARN of the ECR repository."
  value       = aws_ecr_repository.platform.arn
}

output "repository_name" {
  description = "Name of the ECR repository."
  value       = aws_ecr_repository.platform.name
}
