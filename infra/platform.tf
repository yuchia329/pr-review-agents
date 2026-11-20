# What the cluster runs after the nodes exist. Strict network policy is already on, so each
# of these namespaces has a policy. The sandbox namespace is the one KubernetesSandbox uses
# until a Tenant has its own: install-labelled pods may use the network, and every other pod
# in the namespace may not.

# RDS for PostgreSQL 16 refuses connections without TLS (rds.force_ssl). The connection is
# encrypted, but the server certificate is not checked, since the RDS CA bundle is not mounted.
locals {
  rds_tls = { enabled = true, enableHostVerification = false }
}

resource "kubernetes_namespace" "temporal" {
  metadata {
    name = "temporal"
  }

  depends_on = [aws_eks_node_group.system, aws_eks_addon.vpc_cni]
}

resource "kubernetes_namespace" "sandbox" {
  metadata {
    name = "sandbox"
  }

  depends_on = [aws_eks_node_group.sandbox, aws_eks_addon.vpc_cni]
}

resource "kubernetes_secret" "temporal_db" {
  metadata {
    name      = "temporal-db"
    namespace = kubernetes_namespace.temporal.metadata[0].name
  }

  data = {
    password = random_password.db.result
  }
}

resource "helm_release" "temporal" {
  name       = "temporal"
  repository = "https://go.temporal.io/helm-charts"
  chart      = "temporal"
  version    = "1.8.0"
  namespace  = kubernetes_namespace.temporal.metadata[0].name

  timeout       = 900
  wait          = true
  wait_for_jobs = true

  values = [
    yamlencode({
      server = {
        replicaCount = 1
        nodeSelector = {
          (local.node_label) = local.system_label
        }
        resources = {
          requests = { cpu = "100m", memory = "256Mi" }
          limits   = { memory = "1Gi" }
        }
        config = {
          namespaces = {
            create    = true
            namespace = [{ name = "default", retention = "3d" }]
          }
          persistence = {
            defaultStore     = "default"
            visibilityStore  = "visibility"
            numHistoryShards = 512
            datastores = {
              default = {
                sql = {
                  createDatabase  = true
                  manageSchema    = true
                  defaultDb       = "postgres"
                  pluginName      = "postgres12"
                  driverName      = "postgres12"
                  databaseName    = "temporal"
                  connectAddr     = "${aws_db_instance.temporal.address}:5432"
                  connectProtocol = "tcp"
                  tls             = local.rds_tls
                  user            = "temporal"
                  existingSecret  = kubernetes_secret.temporal_db.metadata[0].name
                  secretKey       = "password"
                  maxConns        = 20
                  maxIdleConns    = 20
                  maxConnLifetime = "1h"
                }
              }
              visibility = {
                sql = {
                  createDatabase  = true
                  manageSchema    = true
                  defaultDb       = "postgres"
                  pluginName      = "postgres12"
                  driverName      = "postgres12"
                  databaseName    = "temporal_visibility"
                  connectAddr     = "${aws_db_instance.temporal.address}:5432"
                  connectProtocol = "tcp"
                  tls             = local.rds_tls
                  user            = "temporal"
                  existingSecret  = kubernetes_secret.temporal_db.metadata[0].name
                  secretKey       = "password"
                  maxConns        = 20
                  maxIdleConns    = 20
                  maxConnLifetime = "1h"
                }
              }
            }
          }
        }
      }
      admintools = {
        nodeSelector = {
          (local.node_label) = local.system_label
        }
      }
      web = {
        nodeSelector = {
          (local.node_label) = local.system_label
        }
      }
      schema = {
        useHelmHooks = true
      }
    })
  ]

  depends_on = [
    aws_eks_addon.coredns,
    kubernetes_network_policy.temporal,
    aws_db_instance.temporal,
  ]
}

resource "kubernetes_network_policy" "coredns" {
  metadata {
    name      = "coredns"
    namespace = "kube-system"
  }

  spec {
    pod_selector {
      match_labels = {
        "k8s-app" = "kube-dns"
      }
    }

    policy_types = ["Ingress", "Egress"]

    ingress {
      from {
        ip_block {
          cidr = "0.0.0.0/0"
        }
      }

      ports {
        port     = "53"
        protocol = "UDP"
      }

      ports {
        port     = "53"
        protocol = "TCP"
      }
    }

    egress {
      to {
        ip_block {
          cidr = "${local.vpc_dns}/32"
        }
      }

      ports {
        port     = "53"
        protocol = "UDP"
      }

      ports {
        port     = "53"
        protocol = "TCP"
      }
    }

    egress {
      to {
        ip_block {
          cidr = var.service_cidr
        }
      }

      ports {
        port     = "443"
        protocol = "TCP"
      }
    }

    egress {
      to {
        ip_block {
          cidr = var.vpc_cidr
        }
      }

      ports {
        port     = "443"
        protocol = "TCP"
      }
    }
  }

  depends_on = [aws_eks_addon.vpc_cni, aws_eks_node_group.system]
}

resource "kubernetes_network_policy" "temporal" {
  metadata {
    name      = "temporal"
    namespace = kubernetes_namespace.temporal.metadata[0].name
  }

  spec {
    pod_selector {}

    policy_types = ["Ingress", "Egress"]

    ingress {
      from {
        pod_selector {}
      }
    }

    ingress {
      from {
        ip_block {
          cidr = local.public_cidrs[0]
        }
      }

      from {
        ip_block {
          cidr = local.public_cidrs[1]
        }
      }

      ports {
        port     = "7233"
        protocol = "TCP"
      }
    }

    egress {
      to {
        pod_selector {}
      }
    }

    egress {
      to {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "kube-system"
          }
        }
      }

      ports {
        port     = "53"
        protocol = "UDP"
      }

      ports {
        port     = "53"
        protocol = "TCP"
      }
    }

    egress {
      to {
        ip_block {
          cidr = local.public_cidrs[0]
        }
      }

      to {
        ip_block {
          cidr = local.public_cidrs[1]
        }
      }

      ports {
        port     = "5432"
        protocol = "TCP"
      }
    }

    egress {
      to {
        ip_block {
          cidr = var.service_cidr
        }
      }

      ports {
        port     = "443"
        protocol = "TCP"
      }
    }
  }
}

resource "kubernetes_network_policy" "sandbox_deny" {
  metadata {
    name      = "deny-all"
    namespace = kubernetes_namespace.sandbox.metadata[0].name
  }

  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}

resource "kubernetes_network_policy" "sandbox_install" {
  metadata {
    name      = "install-may-use-the-network"
    namespace = kubernetes_namespace.sandbox.metadata[0].name
  }

  spec {
    pod_selector {
      match_labels = {
        phase = "install"
      }
    }

    policy_types = ["Egress"]

    egress {
      to {
        ip_block {
          cidr = "0.0.0.0/0"
        }
      }
    }
  }
}

resource "kubernetes_namespace" "tailscale" {
  count = var.tailscale_auth_key == "" ? 0 : 1

  metadata {
    name = "tailscale"
  }

  depends_on = [aws_eks_addon.vpc_cni, aws_eks_node_group.system]
}

resource "kubernetes_secret" "tailscale" {
  count = var.tailscale_auth_key == "" ? 0 : 1

  metadata {
    name      = "tailscale-auth"
    namespace = kubernetes_namespace.tailscale[0].metadata[0].name
  }

  data = {
    TS_AUTHKEY = var.tailscale_auth_key
  }
}

resource "kubernetes_deployment" "tailscale" {
  count = var.tailscale_auth_key == "" ? 0 : 1

  metadata {
    name      = "tailscale"
    namespace = kubernetes_namespace.tailscale[0].metadata[0].name
  }

  spec {
    replicas = 1

    selector {
      match_labels = {
        app = "tailscale"
      }
    }

    template {
      metadata {
        labels = {
          app = "tailscale"
        }
      }

      spec {
        node_selector = {
          (local.node_label) = local.system_label
        }

        container {
          name  = "tailscale"
          image = "tailscale/tailscale:v1.86.2"

          env {
            name = "TS_AUTHKEY"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.tailscale[0].metadata[0].name
                key  = "TS_AUTHKEY"
              }
            }
          }

          env {
            name  = "TS_HOSTNAME"
            value = var.cluster_name
          }

          env {
            name  = "TS_USERSPACE"
            value = "true"
          }

          env {
            name  = "TS_OUTBOUND_HTTP_PROXY_LISTEN"
            value = "0.0.0.0:8080"
          }

          env {
            name  = "TS_STATE_DIR"
            value = "/var/lib/tailscale"
          }

          port {
            name           = "proxy"
            container_port = 8080
          }

          volume_mount {
            name       = "state"
            mount_path = "/var/lib/tailscale"
          }
        }

        volume {
          name = "state"
          empty_dir {}
        }
      }
    }
  }
}

resource "kubernetes_service" "tailscale" {
  count = var.tailscale_auth_key == "" ? 0 : 1

  metadata {
    name      = "tailscale"
    namespace = kubernetes_namespace.tailscale[0].metadata[0].name
  }

  spec {
    selector = {
      app = "tailscale"
    }

    port {
      port        = 8080
      target_port = 8080
    }
  }
}

resource "kubernetes_network_policy" "tailscale" {
  count = var.tailscale_auth_key == "" ? 0 : 1

  metadata {
    name      = "tailscale"
    namespace = kubernetes_namespace.tailscale[0].metadata[0].name
  }

  spec {
    pod_selector {
      match_labels = {
        app = "tailscale"
      }
    }

    policy_types = ["Ingress", "Egress"]

    ingress {
      from {
        ip_block {
          cidr = var.vpc_cidr
        }
      }

      ports {
        port     = "8080"
        protocol = "TCP"
      }
    }

    egress {
      to {
        ip_block {
          cidr = "0.0.0.0/0"
        }
      }
    }
  }
}
