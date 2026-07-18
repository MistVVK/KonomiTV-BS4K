group "default" {
  targets = [
    "cuda12-4-amd",
    "cuda12-4-no-amd",
    "cuda12-8-amd",
    "cuda12-8-no-amd",
  ]
}

target "common" {
  context    = "."
  dockerfile = "Dockerfile"
}

target "cuda12-4-amd" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12-4"
    INSTALL_AMD   = "true"
  }
  tags = ["konomitv:cuda12.4-amd"]
}

target "cuda12-4-no-amd" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12-4"
    INSTALL_AMD   = "false"
  }
  tags = ["konomitv:cuda12.4-no-amd"]
}

target "cuda12-8-amd" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12-8"
    INSTALL_AMD   = "true"
  }
  tags = ["konomitv:cuda12.8-amd"]
}

target "cuda12-8-no-amd" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12-8"
    INSTALL_AMD   = "false"
  }
  tags = ["konomitv:cuda12.8-no-amd"]
}
