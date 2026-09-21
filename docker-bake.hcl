group "default" {
  targets = [
    "cuda12-4-nonfree",
    "cuda12-4-intel",
    "cuda12-4-amd",
    "cuda12-4-free",
    "cuda12-8-nonfree",
    "cuda12-8-intel",
    "cuda12-8-amd",
    "cuda12-8-free",
  ]
}

target "common" {
  context    = "."
  dockerfile = "Dockerfile"
}

# NONFREE は再配布可否プロファイル (nonfree / free)。ベンダー選択は INTEL_NONFREE / AMD_NONFREE build arg
# が決める。nonfree 系のプロファイルではベンダーを明示し、free は両方 false を必須とする。

target "cuda12-4-nonfree" {
  inherits = ["common"]
  args = {
    CUDA_VERSION  = "12.4"
    NONFREE       = "nonfree"
    INTEL_NONFREE = "true"
    AMD_NONFREE   = "true"
  }
  tags = ["konomitv-bs4k:cuda12.4-nonfree"]
}

target "cuda12-4-intel" {
  inherits = ["common"]
  args = {
    CUDA_VERSION  = "12.4"
    NONFREE       = "nonfree"
    INTEL_NONFREE = "true"
    AMD_NONFREE   = "false"
  }
  tags = ["konomitv-bs4k:cuda12.4-intel"]
}

target "cuda12-4-amd" {
  inherits = ["common"]
  args = {
    CUDA_VERSION  = "12.4"
    NONFREE       = "nonfree"
    INTEL_NONFREE = "false"
    AMD_NONFREE   = "true"
  }
  tags = ["konomitv-bs4k:cuda12.4-amd"]
}

target "cuda12-4-free" {
  inherits = ["common"]
  args = {
    CUDA_VERSION  = "12.4"
    NONFREE       = "free"
    INTEL_NONFREE = "false"
    AMD_NONFREE   = "false"
  }
  tags = ["konomitv-bs4k:cuda12.4-free"]
}

target "cuda12-8-nonfree" {
  inherits = ["common"]
  args = {
    CUDA_VERSION  = "12.8"
    NONFREE       = "nonfree"
    INTEL_NONFREE = "true"
    AMD_NONFREE   = "true"
  }
  tags = ["konomitv-bs4k:cuda12.8-nonfree"]
}

target "cuda12-8-intel" {
  inherits = ["common"]
  args = {
    CUDA_VERSION  = "12.8"
    NONFREE       = "nonfree"
    INTEL_NONFREE = "true"
    AMD_NONFREE   = "false"
  }
  tags = ["konomitv-bs4k:cuda12.8-intel"]
}

target "cuda12-8-amd" {
  inherits = ["common"]
  args = {
    CUDA_VERSION  = "12.8"
    NONFREE       = "nonfree"
    INTEL_NONFREE = "false"
    AMD_NONFREE   = "true"
  }
  tags = ["konomitv-bs4k:cuda12.8-amd"]
}

target "cuda12-8-free" {
  inherits = ["common"]
  args = {
    CUDA_VERSION  = "12.8"
    NONFREE       = "free"
    INTEL_NONFREE = "false"
    AMD_NONFREE   = "false"
  }
  tags = ["konomitv-bs4k:cuda12.8-free"]
}
