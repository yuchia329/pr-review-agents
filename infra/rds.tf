resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "temporal" {
  name       = "${var.cluster_name}-temporal"
  subnet_ids = aws_subnet.public[*].id

  tags = {
    Name = "${var.cluster_name}-temporal"
  }
}

resource "aws_security_group" "rds" {
  name        = "${var.cluster_name}-temporal"
  description = "Postgres for Temporal, from the cluster security group only."
  vpc_id      = aws_vpc.this.id

  tags = {
    Name = "${var.cluster_name}-temporal"
  }
}

resource "aws_vpc_security_group_ingress_rule" "rds" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "Temporal on the system node"
}

resource "aws_vpc_security_group_egress_rule" "rds" {
  security_group_id = aws_security_group.rds.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
  description       = "Replies"
}

resource "aws_db_instance" "temporal" {
  identifier     = "${var.cluster_name}-temporal"
  engine         = "postgres"
  engine_version = "16.15"
  instance_class = "db.t4g.small"

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_subnet_group_name   = aws_db_subnet_group.temporal.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = false

  username = "temporal"
  password = random_password.db.result

  backup_retention_period = 1
  skip_final_snapshot     = true
  deletion_protection     = false
  apply_immediately       = true

  tags = {
    Name = "${var.cluster_name}-temporal"
  }
}
