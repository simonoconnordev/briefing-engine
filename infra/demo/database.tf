# RDS Postgres. Small, single-AZ, encrypted, not reachable from the internet.
#
# RDS creates and keeps the master password in Secrets Manager
# (manage_master_user_password), so it is never in Terraform state or in this repository.
# ECS hands it to the containers as DB_PASSWORD, and the app builds its URL from the parts.
#
# Demo mode: the database is disposable. `demo-down.ps1` destroys it with no final
# snapshot; my real reading history stays in the local Postgres on my machine.
# Note: RDS rotates the managed password every 7 days. A demo left up longer than that
# needs its tasks restarted after a rotation (they read the secret at start-up).

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = var.postgres_version
  instance_class = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name                     = "briefing"
  username                    = "briefing"
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = false # the production setting is true

  backup_retention_period      = 1
  auto_minor_version_upgrade   = true
  performance_insights_enabled = false
  apply_immediately            = true

  deletion_protection = false
  skip_final_snapshot = true
}
