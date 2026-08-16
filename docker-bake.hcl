group "default" {
  targets = [
    "cuda12-4-nonfree",
    "cuda12-4-free",
    "cuda12-8-nonfree",
    "cuda12-8-free",
  ]
}

group "all" {
  targets = [
    "cuda12-4-nonfree",
    "cuda12-4-intel-nonfree",
    "cuda12-4-amd-nonfree",
    "cuda12-4-free",
    "cuda12-8-nonfree",
    "cuda12-8-intel-nonfree",
    "cuda12-8-amd-nonfree",
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
    NONFREE      = "nonfree"
  }
  tags = ["konomitv-bs4k:cuda12.4-nonfree"]
}

target "cuda12-4-intel-nonfree" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12.4"
    NONFREE      = "intel-nonfree"
  }
  tags = ["konomitv-bs4k:cuda12.4-intel-nonfree"]
}

target "cuda12-4-amd-nonfree" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12.4"
    NONFREE      = "amd-nonfree"
  }
  tags = ["konomitv-bs4k:cuda12.4-amd-nonfree"]
}

target "cuda12-4-free" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12.4"
    NONFREE      = "free"
  }
  tags = ["konomitv-bs4k:cuda12.4-free"]
}

target "cuda12-8-nonfree" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12.8"
    NONFREE      = "nonfree"
  }
  tags = ["konomitv-bs4k:cuda12.8-nonfree"]
}

target "cuda12-8-intel-nonfree" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12.8"
    NONFREE      = "intel-nonfree"
  }
  tags = ["konomitv-bs4k:cuda12.8-intel-nonfree"]
}

target "cuda12-8-amd-nonfree" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12.8"
    NONFREE      = "amd-nonfree"
  }
  tags = ["konomitv-bs4k:cuda12.8-amd-nonfree"]
}

target "cuda12-8-free" {
  inherits = ["common"]
  args = {
    CUDA_VERSION = "12.8"
    NONFREE      = "free"
  }
  tags = ["konomitv-bs4k:cuda12.8-free"]
}
