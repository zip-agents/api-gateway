# The gateway's accounts database: Aurora PostgreSQL Serverless v2, scaling to 0 ACU and
# pausing ~15 min after the last query (about $1/month for storage). The first query
# after that waits for it to resume: ~15 s, 30 s or more after a day paused.
# Reachable from the gateway tasks only. Destroyed with this stack, data included.
#
# The password never enters Terraform state: it is an ephemeral value, written once to
# the cluster and to Secrets Manager through write-only arguments. To change it, bump
# local.database_password_version and apply; then restart the gateway.

locals {
  database_name             = "gateway"
  database_user             = "gateway"
  database_password_version = 1
}

ephemeral "random_password" "database" {
  length  = 40
  special = false # no URL or shell quoting to get wrong
}

# The newest Aurora PostgreSQL 16 at creation; AWS applies minor upgrades after that.
data "aws_rds_engine_version" "postgres" {
  engine  = "aurora-postgresql"
  version = "16"
  latest  = true
}

resource "aws_db_subnet_group" "accounts" {
  name        = "${local.name}-accounts"
  description = "Private subnets for the accounts database"
  subnet_ids  = aws_subnet.private[*].id
}

resource "aws_security_group" "database" {
  name        = "${local.name}-database"
  description = "Accounts database: reachable from the gateway tasks only"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "database_from_gateway" {
  security_group_id            = aws_security_group.database.id
  description                  = "Postgres from the gateway"
  referenced_security_group_id = aws_security_group.gateway.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_rds_cluster" "accounts" {
  cluster_identifier = "${local.name}-accounts"
  engine             = "aurora-postgresql"
  engine_mode        = "provisioned" # Serverless v2 is provisioned + db.serverless instances
  engine_version     = data.aws_rds_engine_version.postgres.version_actual
  database_name      = local.database_name
  master_username    = local.database_user

  master_password_wo         = ephemeral.random_password.database.result
  master_password_wo_version = local.database_password_version

  db_subnet_group_name   = aws_db_subnet_group.accounts.name
  vpc_security_group_ids = [aws_security_group.database.id]
  storage_encrypted      = true

  serverlessv2_scaling_configuration {
    min_capacity             = 0 # pause when idle
    max_capacity             = 2
    seconds_until_auto_pause = 600 # the gateway closes idle connections after 300 s (store.py)
  }

  backup_retention_period = 1
  copy_tags_to_snapshot   = true
  skip_final_snapshot     = true  # teardown must always succeed; the data is disposable
  deletion_protection     = false # same
  apply_immediately       = true

  lifecycle {
    ignore_changes = [engine_version] # AWS's minor upgrades are not drift
  }
}

resource "aws_rds_cluster_instance" "accounts" {
  identifier          = "${local.name}-accounts-1"
  cluster_identifier  = aws_rds_cluster.accounts.id
  instance_class      = "db.serverless"
  engine              = aws_rds_cluster.accounts.engine
  engine_version      = aws_rds_cluster.accounts.engine_version
  publicly_accessible = false

  auto_minor_version_upgrade = true

  lifecycle {
    ignore_changes = [engine_version]
  }
}

# The gateway reads the password from here (GATEWAY_DB_PASSWORD).
resource "aws_secretsmanager_secret" "database" {
  name                    = "${local.project}/${local.environment}/database"
  description             = "password of the gateway's Aurora user."
  recovery_window_in_days = 0 # teardown deletes it at once
}

resource "aws_secretsmanager_secret_version" "database" {
  secret_id                = aws_secretsmanager_secret.database.id
  secret_string_wo         = jsonencode({ password = ephemeral.random_password.database.result })
  secret_string_wo_version = local.database_password_version
}
