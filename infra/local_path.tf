# local-path provisioner, pinned to v0.0.32. Volumes land on the sandbox node's data disk.
# The size in a claim is ignored by this provisioner. The helper tolerates the sandbox taint,
# because that is the node the claim is bound to.

resource "kubernetes_namespace" "local_path" {
  metadata {
    name = "local-path-storage"
  }

  depends_on = [aws_eks_addon.vpc_cni, aws_eks_node_group.system]
}

resource "kubernetes_service_account" "local_path" {
  metadata {
    name      = "local-path-provisioner-service-account"
    namespace = kubernetes_namespace.local_path.metadata[0].name
  }
}

resource "kubernetes_role" "local_path" {
  metadata {
    name      = "local-path-provisioner-role"
    namespace = kubernetes_namespace.local_path.metadata[0].name
  }

  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list", "watch", "create", "patch", "update", "delete"]
  }
}

resource "kubernetes_cluster_role" "local_path" {
  metadata {
    name = "local-path-provisioner-role"
  }

  rule {
    api_groups = [""]
    resources  = ["nodes", "persistentvolumeclaims", "configmaps", "pods", "pods/log"]
    verbs      = ["get", "list", "watch"]
  }

  rule {
    api_groups = [""]
    resources  = ["persistentvolumes"]
    verbs      = ["get", "list", "watch", "create", "patch", "update", "delete"]
  }

  rule {
    api_groups = [""]
    resources  = ["events"]
    verbs      = ["create", "patch"]
  }

  rule {
    api_groups = ["storage.k8s.io"]
    resources  = ["storageclasses"]
    verbs      = ["get", "list", "watch"]
  }
}

resource "kubernetes_role_binding" "local_path" {
  metadata {
    name      = "local-path-provisioner-bind"
    namespace = kubernetes_namespace.local_path.metadata[0].name
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.local_path.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.local_path.metadata[0].name
    namespace = kubernetes_namespace.local_path.metadata[0].name
  }
}

resource "kubernetes_cluster_role_binding" "local_path" {
  metadata {
    name = "local-path-provisioner-bind"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role.local_path.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.local_path.metadata[0].name
    namespace = kubernetes_namespace.local_path.metadata[0].name
  }
}

resource "kubernetes_config_map" "local_path" {
  metadata {
    name      = "local-path-config"
    namespace = kubernetes_namespace.local_path.metadata[0].name
  }

  data = {
    "config.json" = jsonencode({
      nodePathMap = [{
        node  = "DEFAULT_PATH_FOR_NON_LISTED_NODES"
        paths = ["/mnt/sandbox-data"]
      }]
    })
    setup    = "#!/bin/sh\nset -eu\nmkdir -m 0777 -p \"$VOL_DIR\"\n"
    teardown = "#!/bin/sh\nset -eu\nrm -rf \"$VOL_DIR\"\n"
    "helperPod.yaml" = yamlencode({
      apiVersion = "v1"
      kind       = "Pod"
      metadata   = { name = "helper-pod" }
      spec = {
        tolerations = [
          { key = "sandbox", operator = "Equal", value = "true", effect = "NoSchedule" },
          { key = "node.kubernetes.io/disk-pressure", operator = "Exists", effect = "NoSchedule" },
        ]
        containers = [{
          name            = "helper-pod"
          image           = "public.ecr.aws/docker/library/busybox:1.36"
          imagePullPolicy = "IfNotPresent"
        }]
      }
    })
  }
}

resource "kubernetes_storage_class" "sandbox" {
  metadata {
    name = "sandbox"
  }

  storage_provisioner = "rancher.io/local-path"
  volume_binding_mode = "WaitForFirstConsumer"
  reclaim_policy      = "Delete"
}

resource "kubernetes_deployment" "local_path" {
  metadata {
    name      = "local-path-provisioner"
    namespace = kubernetes_namespace.local_path.metadata[0].name
  }

  spec {
    replicas = 1

    selector {
      match_labels = {
        app = "local-path-provisioner"
      }
    }

    template {
      metadata {
        labels = {
          app = "local-path-provisioner"
        }
      }

      spec {
        service_account_name = kubernetes_service_account.local_path.metadata[0].name

        node_selector = {
          (local.node_label) = local.system_label
        }

        container {
          name  = "local-path-provisioner"
          image = "rancher/local-path-provisioner:v0.0.32"

          command = ["local-path-provisioner", "--debug", "start", "--config", "/etc/config/config.json"]

          env {
            name = "POD_NAMESPACE"
            value_from {
              field_ref {
                field_path = "metadata.namespace"
              }
            }
          }

          env {
            name  = "CONFIG_MOUNT_PATH"
            value = "/etc/config/"
          }

          volume_mount {
            name       = "config-volume"
            mount_path = "/etc/config/"
          }
        }

        volume {
          name = "config-volume"
          config_map {
            name = kubernetes_config_map.local_path.metadata[0].name
          }
        }
      }
    }
  }

  depends_on = [
    kubernetes_role_binding.local_path,
    kubernetes_cluster_role_binding.local_path,
    kubernetes_network_policy.local_path,
  ]
}

resource "kubernetes_network_policy" "local_path" {
  metadata {
    name      = "local-path"
    namespace = kubernetes_namespace.local_path.metadata[0].name
  }

  spec {
    pod_selector {
      match_labels = {
        app = "local-path-provisioner"
      }
    }

    policy_types = ["Egress"]

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
}
