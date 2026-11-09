variable "image_tag" {
  description = "Tag of the image in ECR to run (a git SHA; demo-up.ps1 pushes it first)."
  type        = string
}

variable "api_desired_count" {
  type    = number
  default = 1
}

variable "worker_desired_count" {
  type    = number
  default = 1
}

variable "task_cpu" {
  description = "Fargate CPU units per task (256 = 0.25 vCPU)."
  type        = number
  default     = 256
}

variable "task_memory" {
  description = "Fargate memory per task, in MiB."
  type        = number
  default     = 512
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "postgres_version" {
  description = "Major version; RDS picks the current minor and keeps it patched."
  type        = string
  default     = "17"
}

variable "log_retention_days" {
  type    = number
  default = 30
}
