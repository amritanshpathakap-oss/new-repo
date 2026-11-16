data "aws_availability_zones" "available" { state = "available" }
module "vpc" {
  source               = "terraform-aws-modules/vpc/aws"
  version              = "5.19.0"
  name                 = var.name
  cidr                 = "10.42.0.0/16"
  azs                  = slice(data.aws_availability_zones.available.names, 0, 3)
  private_subnets      = ["10.42.0.0/20", "10.42.16.0/20", "10.42.32.0/20"]
  public_subnets       = ["10.42.128.0/24", "10.42.129.0/24", "10.42.130.0/24"]
  enable_nat_gateway   = true
  single_nat_gateway   = true # demo cost tradeoff; use one per AZ for production
  enable_dns_hostnames = true
  public_subnet_tags   = { "kubernetes.io/role/elb" = "1" }
  private_subnet_tags  = { "kubernetes.io/role/internal-elb" = "1" }
}
module "eks" {
  source                                   = "terraform-aws-modules/eks/aws"
  version                                  = "20.36.0"
  cluster_name                             = var.name
  cluster_version                          = var.kubernetes_version
  cluster_endpoint_public_access           = true
  cluster_endpoint_public_access_cidrs     = var.admin_cidrs
  enable_cluster_creator_admin_permissions = true
  vpc_id                                   = module.vpc.vpc_id
  subnet_ids                               = module.vpc.private_subnets
  cluster_addons                           = { coredns = {}, kube-proxy = {}, vpc-cni = {} }
  eks_managed_node_groups = {
    general = {
      instance_types = ["t3.medium"]
      min_size       = 2
      max_size       = 5
      desired_size   = 2
      disk_size      = 30
    }
  }
}
resource "aws_ecr_repository" "app" {
  name = var.name
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
  image_tag_mutability = "IMMUTABLE"
}
output "cluster_name" { value = module.eks.cluster_name }
output "ecr_repository_url" { value = aws_ecr_repository.app.repository_url }
output "region" { value = var.region }
data "aws_iam_policy_document" "ebs_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${replace(module.eks.cluster_oidc_issuer_url, "https://", "")}:sub"
      values   = ["system:serviceaccount:kube-system:ebs-csi-controller-sa"]
    }
    condition {
      test     = "StringEquals"
      variable = "${replace(module.eks.cluster_oidc_issuer_url, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "ebs_csi" {
  name               = "${var.name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.ebs_assume.json
}
resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}
resource "aws_eks_addon" "ebs_csi" {
  cluster_name             = module.eks.cluster_name
  addon_name               = "aws-ebs-csi-driver"
  service_account_role_arn = aws_iam_role.ebs_csi.arn
  depends_on               = [aws_iam_role_policy_attachment.ebs_csi]
}
