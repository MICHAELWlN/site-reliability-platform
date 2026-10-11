output "security_group_id" {
  description = "ID of the demonstration security group."
  value       = aws_security_group.demo.id
}

output "security_group_name" {
  description = "Name of the demonstration security group."
  value       = aws_security_group.demo.name
}
