group "default" {
  targets = [
    "cuda12-4-nonfree",
    "cuda12-4-free",
    "cuda12-8-nonfree",
    "cuda12-8-free",
  ]
}

target "common" {
  context    = "."
  dockerfile = "Dockerfile"
}

target "cuda12-4-nonfree" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12.4"
    NONFREE      = "true"
  }
  tags = ["konomitv:cuda12.4-nonfree"]
}

target "cuda12-4-free" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12.4"
    NONFREE      = "false"
  }
  tags = ["konomitv:cuda12.4-free"]
}

target "cuda12-8-nonfree" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12.8"
    NONFREE      = "true"
  }
  tags = ["konomitv:cuda12.8-nonfree"]
}

target "cuda12-8-free" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12.8"
    NONFREE      = "false"
  }
  tags = ["konomitv:cuda12.8-free"]
}
