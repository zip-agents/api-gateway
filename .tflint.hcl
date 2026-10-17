# tflint configuration for terraform/ (make tf-check). The bundled Terraform rules only:
# no plugin downloads, so it runs offline in CI and locally.
plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
