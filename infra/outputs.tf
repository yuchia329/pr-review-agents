output "region" {
  value = var.region
}

output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "configure_kubectl" {
  value = "aws eks update-kubeconfig --name ${aws_eks_cluster.this.name} --region ${var.region}"
}

output "rds_endpoint" {
  value = aws_db_instance.temporal.address
}

output "temporal_frontend" {
  value = "temporal-frontend.temporal.svc.cluster.local:7233"
}

output "sandbox_namespace" {
  value = kubernetes_namespace.sandbox.metadata[0].name
}

output "sandbox_storage_class" {
  value = kubernetes_storage_class.sandbox.metadata[0].name
}

output "tailscale_proxy" {
  value = "http://tailscale.tailscale.svc:8080"
}
