variable "region" {
  type        = string
  description = "AWS region. Graviton node types in the plan are offered here."
  default     = "us-east-1"
}

variable "cluster_name" {
  type        = string
  description = "Name of the EKS cluster, the node groups, and the RDS instance prefix."
  default     = "pr-verification"
}

variable "kubernetes_version" {
  type        = string
  description = "EKS Kubernetes version. Addon versions in eks.tf are the current ones for 1.34."
  default     = "1.34"
}

variable "vpc_cidr" {
  type        = string
  description = "CIDR of the VPC. Private subnets for the nodes and RDS are cut from it."
  default     = "10.0.0.0/16"
}

variable "service_cidr" {
  type        = string
  description = "CIDR of Kubernetes service addresses. CoreDNS and Temporal are allowed to reach it."
  default     = "172.20.0.0/16"
}

variable "api_cidrs" {
  type        = list(string)
  description = "CIDRs allowed to call the public Kubernetes API. Narrow this to the addresses you apply from."
  default     = ["0.0.0.0/0"]
}

variable "budget_email" {
  type        = string
  description = "Address that receives the $75 monthly budget alarm."

  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+$", var.budget_email))
    error_message = "budget_email must be an email address."
  }
}

variable "sandbox_pod_pids_limit" {
  type        = number
  description = "podPidsLimit on the sandbox node. The plan names the setting and not the number; 4096 is the bound used here."
  default     = 4096
}

variable "sandbox_data_gib" {
  type        = number
  description = "Size of the gp3 data disk on the sandbox node. Sandbox volumes live here, off the node root disk."
  default     = 100
}

variable "tailscale_auth_key" {
  type        = string
  description = "Tailscale auth key for the proxy on the system node. Empty leaves the proxy uninstalled. Workers reach vLLM through http://tailscale.tailscale.svc:8080."
  default     = ""
  sensitive   = true
}
