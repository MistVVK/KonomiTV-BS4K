#define _GNU_SOURCE

/*
 * 録画シリーズ ACP subprocess 向けの Landlock launcher。
 *
 * KonomiTV サーバーと各 provider は同じ UID で動くため、0600 / 0700 の DAC だけでは
 * provider 間の資格情報を分離できない。この launcher は provider を exec する直前に
 * deny-by-default の Landlock domain を作り、選択中 profile だけを書込み可能にする。
 *
 * 失敗時に未隔離の command を起動する fallback は設けない。固定エラーだけを stderr へ
 * 出し、入力 path や資格情報をログへ含めない。
 */

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/landlock.h>
#include <linux/openat2.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef LANDLOCK_CREATE_RULESET_VERSION
#define LANDLOCK_CREATE_RULESET_VERSION 1
#endif

#ifndef LANDLOCK_ACCESS_FS_REFER
#define LANDLOCK_ACCESS_FS_REFER (1ULL << 13)
#endif

#ifndef LANDLOCK_ACCESS_FS_TRUNCATE
#define LANDLOCK_ACCESS_FS_TRUNCATE (1ULL << 14)
#endif

#ifndef LANDLOCK_ACCESS_FS_IOCTL_DEV
#define LANDLOCK_ACCESS_FS_IOCTL_DEV (1ULL << 15)
#endif

#ifndef LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET
#define LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET (1ULL << 0)
#endif

#ifndef LANDLOCK_SCOPE_SIGNAL
#define LANDLOCK_SCOPE_SIGNAL (1ULL << 1)
#endif

#if !defined(__NR_landlock_create_ruleset) || \
    !defined(__NR_landlock_add_rule) || \
    !defined(__NR_landlock_restrict_self) || \
    !defined(__NR_openat2)
#error "Required Landlock/openat2 system call numbers are unavailable."
#endif

#define ACP_SANDBOX_MINIMUM_ABI 6
#define ACP_SANDBOX_MAX_READ_FILES 4

/*
 * linux-libc-dev が古い builder でも ABI 6 の ruleset を表現できるよう、
 * kernel UAPI と同じ field layout をこの translation unit 内で固定する。
 * filesystem だけでなく同一 UID provider から親 process への signal と
 * abstract UNIX socket 接続も遮断するため、ABI 6 未満では起動しない。
 */
struct AcpLandlockRulesetAttr {
    uint64_t handled_access_fs;
    uint64_t handled_access_net;
    uint64_t scoped;
};

enum AcpPathKind {
    ACP_PATH_ANY,
    ACP_PATH_DIRECTORY,
    ACP_PATH_REGULAR_FILE,
};

struct AcpArguments {
    const char *profile;
    const char *working_directory;
    const char *read_files[ACP_SANDBOX_MAX_READ_FILES];
    size_t read_file_count;
    char **command_argv;
};

static const uint64_t ACP_READ_ACCESS =
    LANDLOCK_ACCESS_FS_READ_FILE |
    LANDLOCK_ACCESS_FS_READ_DIR;

static const uint64_t ACP_READ_EXECUTE_ACCESS =
    LANDLOCK_ACCESS_FS_EXECUTE |
    LANDLOCK_ACCESS_FS_READ_FILE |
    LANDLOCK_ACCESS_FS_READ_DIR;

/*
 * profile 内では通常ファイル・directory・socket・FIFO・symlink の状態管理を許可する。
 * 実行権と device node 作成は含めず、provider が profile に生成した payload の exec を防ぐ。
 */
static const uint64_t ACP_PROFILE_ACCESS =
    LANDLOCK_ACCESS_FS_WRITE_FILE |
    LANDLOCK_ACCESS_FS_READ_FILE |
    LANDLOCK_ACCESS_FS_READ_DIR |
    LANDLOCK_ACCESS_FS_REMOVE_DIR |
    LANDLOCK_ACCESS_FS_REMOVE_FILE |
    LANDLOCK_ACCESS_FS_MAKE_DIR |
    LANDLOCK_ACCESS_FS_MAKE_REG |
    LANDLOCK_ACCESS_FS_MAKE_SOCK |
    LANDLOCK_ACCESS_FS_MAKE_FIFO |
    LANDLOCK_ACCESS_FS_MAKE_SYM |
    LANDLOCK_ACCESS_FS_REFER |
    LANDLOCK_ACCESS_FS_TRUNCATE;

static const uint64_t ACP_FILE_ONLY_ACCESS =
    LANDLOCK_ACCESS_FS_EXECUTE |
    LANDLOCK_ACCESS_FS_WRITE_FILE |
    LANDLOCK_ACCESS_FS_READ_FILE |
    LANDLOCK_ACCESS_FS_TRUNCATE |
    LANDLOCK_ACCESS_FS_IOCTL_DEV;

static int LandlockCreateRuleset(
    const void *attributes,
    size_t size,
    uint32_t flags
) {
    return (int) syscall(__NR_landlock_create_ruleset, attributes, size, flags);
}

static int LandlockAddRule(
    int ruleset_file_descriptor,
    enum landlock_rule_type rule_type,
    const void *rule_attributes,
    uint32_t flags
) {
    return (int) syscall(
        __NR_landlock_add_rule,
        ruleset_file_descriptor,
        rule_type,
        rule_attributes,
        flags
    );
}

static int LandlockRestrictSelf(int ruleset_file_descriptor, uint32_t flags) {
    return (int) syscall(
        __NR_landlock_restrict_self,
        ruleset_file_descriptor,
        flags
    );
}

static int OpenPathWithoutSymlinks(const char *path) {
    /*
     * アプリ管理下の profile / cwd / credential は全 component で
     * symlink と procfs magic link を拒否する。rule 作成前のすり替え余地を狭めるため、
     * realpath() の文字列比較ではなく openat2() で対象 inode を直接取得する。
     */
    const struct open_how how = {
        .flags = O_PATH | O_CLOEXEC,
        .resolve = RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS,
    };
    return (int) syscall(__NR_openat2, AT_FDCWD, path, &how, sizeof(how));
}

static int OpenDirectoryWithoutSymlinks(const char *path) {
    /*
     * profile は同じ FD から再帰走査するため、O_PATH ではなく directory stream を
     * 開ける read-only FD として取得する。全 component の symlink 拒否条件は維持する。
     */
    const struct open_how how = {
        .flags = O_RDONLY | O_DIRECTORY | O_CLOEXEC,
        .resolve = RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS,
    };
    return (int) syscall(__NR_openat2, AT_FDCWD, path, &how, sizeof(how));
}

static int OpenFixedSystemPath(const char *path) {
    /*
     * /usr や /etc/localtime など root 所有の固定 OS path は distribution の symlink を
     * 許容する。可変 provider path にはこの関数を使わない。
     */
    return open(path, O_PATH | O_CLOEXEC);
}

static bool AddPathRuleFromDescriptor(
    int ruleset_file_descriptor,
    int path_file_descriptor,
    uint64_t allowed_access,
    uint64_t handled_access,
    enum AcpPathKind expected_kind
) {
    struct stat path_stat;
    if (fstat(path_file_descriptor, &path_stat) != 0) {
        return false;
    }
    if (
        (expected_kind == ACP_PATH_DIRECTORY && S_ISDIR(path_stat.st_mode) == 0) ||
        (expected_kind == ACP_PATH_REGULAR_FILE && S_ISREG(path_stat.st_mode) == 0)
    ) {
        return false;
    }

    /*
     * regular file へ READ_DIR / MAKE_* など directory 専用 right を渡すと EINVAL になる。
     * directory 以外は file right だけへ絞り込む。
     */
    if (S_ISDIR(path_stat.st_mode) == 0) {
        allowed_access &= ACP_FILE_ONLY_ACCESS;
    }
    allowed_access &= handled_access;
    if (allowed_access == 0) {
        return false;
    }

    const struct landlock_path_beneath_attr path_beneath = {
        .allowed_access = allowed_access,
        .parent_fd = path_file_descriptor,
    };
    const int add_rule_result = LandlockAddRule(
        ruleset_file_descriptor,
        LANDLOCK_RULE_PATH_BENEATH,
        &path_beneath,
        0
    );
    return add_rule_result == 0;
}

static bool AddPathRule(
    int ruleset_file_descriptor,
    const char *path,
    uint64_t allowed_access,
    uint64_t handled_access,
    enum AcpPathKind expected_kind,
    bool reject_symlinks,
    bool required
) {
    int path_file_descriptor = reject_symlinks
        ? OpenPathWithoutSymlinks(path)
        : OpenFixedSystemPath(path);
    if (path_file_descriptor < 0) {
        if (required == false && (errno == ENOENT || errno == ENOTDIR)) {
            return true;
        }
        return false;
    }

    const bool rule_added = AddPathRuleFromDescriptor(
        ruleset_file_descriptor,
        path_file_descriptor,
        allowed_access,
        handled_access,
        expected_kind
    );
    close(path_file_descriptor);
    return rule_added;
}

static bool ValidateProfileDirectory(int directory_file_descriptor, dev_t profile_device) {
    /*
     * 旧版の未隔離 provider が外部秘密ファイルへの hardlink を profile 内へ残していると、
     * path-beneath rule は同じ inode を profile 所属として許可してしまう。通常ファイルは
     * link count 1 だけを受理し、移行前に作られた alias を provider exec 前に拒否する。
     *
     * profile 内 hardlink も fail-closed で拒否する。正規 CLI が必要とする状態ファイルは
     * atomic rename で管理でき、秘密分離を曖昧にする inode alias を永続状態へ許可しない。
     */
    const int iteration_file_descriptor = dup(directory_file_descriptor);
    if (iteration_file_descriptor < 0) {
        return false;
    }
    DIR *directory = fdopendir(iteration_file_descriptor);
    if (directory == NULL) {
        close(iteration_file_descriptor);
        return false;
    }

    bool valid = true;
    while (valid) {
        errno = 0;
        const struct dirent *entry = readdir(directory);
        if (entry == NULL) {
            valid = errno == 0;
            break;
        }
        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) {
            continue;
        }

        struct stat entry_stat;
        if (
            fstatat(
                directory_file_descriptor,
                entry->d_name,
                &entry_stat,
                AT_SYMLINK_NOFOLLOW
            ) != 0
        ) {
            valid = false;
            break;
        }

        /*
         * provider profile 配下に別 filesystem を差し込む構成は認めない。
         * 同一 UID provider は mount 権限を持たないが、root 側の誤設定も fail-closed にする。
         */
        if (entry_stat.st_dev != profile_device) {
            valid = false;
            break;
        }
        if (S_ISREG(entry_stat.st_mode) && entry_stat.st_nlink != 1) {
            valid = false;
            break;
        }
        if (S_ISDIR(entry_stat.st_mode) == 0) {
            continue;
        }

        const struct open_how directory_open_how = {
            .flags = O_RDONLY | O_DIRECTORY | O_CLOEXEC,
            .resolve =
                RESOLVE_BENEATH |
                RESOLVE_NO_SYMLINKS |
                RESOLVE_NO_MAGICLINKS |
                RESOLVE_NO_XDEV,
        };
        const int child_file_descriptor = (int) syscall(
            __NR_openat2,
            directory_file_descriptor,
            entry->d_name,
            &directory_open_how,
            sizeof(directory_open_how)
        );
        if (child_file_descriptor < 0) {
            valid = false;
            break;
        }
        valid = ValidateProfileDirectory(child_file_descriptor, profile_device);
        close(child_file_descriptor);
    }

    if (closedir(directory) != 0) {
        valid = false;
    }
    return valid;
}

static bool ParseArguments(int argument_count, char **argument_values, struct AcpArguments *arguments) {
    memset(arguments, 0, sizeof(*arguments));

    int argument_index = 1;
    while (argument_index < argument_count) {
        const char *argument = argument_values[argument_index];
        if (strcmp(argument, "--") == 0) {
            argument_index++;
            break;
        }
        if (
            strcmp(argument, "--profile") == 0 ||
            strcmp(argument, "--working-directory") == 0 ||
            strcmp(argument, "--read-file") == 0
        ) {
            if (argument_index + 1 >= argument_count) {
                return false;
            }
            const char *value = argument_values[argument_index + 1];
            if (value[0] != '/') {
                return false;
            }
            if (strcmp(argument, "--profile") == 0) {
                if (arguments->profile != NULL) {
                    return false;
                }
                arguments->profile = value;
            } else if (strcmp(argument, "--working-directory") == 0) {
                if (arguments->working_directory != NULL) {
                    return false;
                }
                arguments->working_directory = value;
            } else {
                if (arguments->read_file_count >= ACP_SANDBOX_MAX_READ_FILES) {
                    return false;
                }
                arguments->read_files[arguments->read_file_count] = value;
                arguments->read_file_count++;
            }
            argument_index += 2;
            continue;
        }
        return false;
    }

    if (
        arguments->profile == NULL ||
        arguments->working_directory == NULL ||
        argument_index >= argument_count ||
        argument_values[argument_index][0] != '/'
    ) {
        return false;
    }
    arguments->command_argv = &argument_values[argument_index];
    return true;
}

static uint64_t BuildHandledFilesystemAccess(int abi_version) {
    uint64_t handled_access =
        LANDLOCK_ACCESS_FS_EXECUTE |
        LANDLOCK_ACCESS_FS_WRITE_FILE |
        LANDLOCK_ACCESS_FS_READ_FILE |
        LANDLOCK_ACCESS_FS_READ_DIR |
        LANDLOCK_ACCESS_FS_REMOVE_DIR |
        LANDLOCK_ACCESS_FS_REMOVE_FILE |
        LANDLOCK_ACCESS_FS_MAKE_CHAR |
        LANDLOCK_ACCESS_FS_MAKE_DIR |
        LANDLOCK_ACCESS_FS_MAKE_REG |
        LANDLOCK_ACCESS_FS_MAKE_SOCK |
        LANDLOCK_ACCESS_FS_MAKE_FIFO |
        LANDLOCK_ACCESS_FS_MAKE_BLOCK |
        LANDLOCK_ACCESS_FS_MAKE_SYM |
        LANDLOCK_ACCESS_FS_REFER |
        LANDLOCK_ACCESS_FS_TRUNCATE;
    if (abi_version >= 5) {
        handled_access |= LANDLOCK_ACCESS_FS_IOCTL_DEV;
    }
    return handled_access;
}

static bool AddFixedRuntimeRules(int ruleset_file_descriptor, uint64_t handled_access) {
    /*
     * 実行 runtime は root 所有・完成イメージ内の不変領域だけを広く読取可能にする。
     * /code、/run、/host-rootfs、/proc はここへ含めず、資格情報や録画への迂回を作らない。
     */
    if (
        AddPathRule(
            ruleset_file_descriptor,
            "/usr",
            ACP_READ_EXECUTE_ACCESS,
            handled_access,
            ACP_PATH_DIRECTORY,
            false,
            true
        ) == false ||
        AddPathRule(
            ruleset_file_descriptor,
            "/opt/konomitv-bs4k-acp",
            ACP_READ_EXECUTE_ACCESS,
            handled_access,
            ACP_PATH_DIRECTORY,
            false,
            false
        ) == false
    ) {
        return false;
    }

    static const char *const read_only_directories[] = {
        "/etc/ssl/certs",
    };
    for (
        size_t index = 0;
        index < sizeof(read_only_directories) / sizeof(read_only_directories[0]);
        index++
    ) {
        if (
            AddPathRule(
                ruleset_file_descriptor,
                read_only_directories[index],
                ACP_READ_ACCESS,
                handled_access,
                ACP_PATH_DIRECTORY,
                false,
                false
            ) == false
        ) {
            return false;
        }
    }

    static const char *const read_only_files[] = {
        "/etc/hosts",
        "/etc/resolv.conf",
        "/etc/nsswitch.conf",
        "/etc/host.conf",
        "/etc/gai.conf",
        "/etc/passwd",
        "/etc/group",
        "/etc/ssl/openssl.cnf",
    };
    for (
        size_t index = 0;
        index < sizeof(read_only_files) / sizeof(read_only_files[0]);
        index++
    ) {
        if (
            AddPathRule(
                ruleset_file_descriptor,
                read_only_files[index],
                ACP_READ_ACCESS,
                handled_access,
                ACP_PATH_ANY,
                false,
                false
            ) == false
        ) {
            return false;
        }
    }

    const uint64_t device_access =
        LANDLOCK_ACCESS_FS_READ_FILE |
        LANDLOCK_ACCESS_FS_WRITE_FILE |
        LANDLOCK_ACCESS_FS_IOCTL_DEV;
    static const char *const devices[] = {
        "/dev/null",
        "/dev/urandom",
    };
    for (size_t index = 0; index < sizeof(devices) / sizeof(devices[0]); index++) {
        if (
            AddPathRule(
                ruleset_file_descriptor,
                devices[index],
                device_access,
                handled_access,
                ACP_PATH_ANY,
                false,
                true
            ) == false
        ) {
            return false;
        }
    }
    return true;
}

static bool ApplySandbox(const struct AcpArguments *arguments) {
    const int abi_version = LandlockCreateRuleset(
        NULL,
        0,
        LANDLOCK_CREATE_RULESET_VERSION
    );
    if (abi_version < ACP_SANDBOX_MINIMUM_ABI) {
        return false;
    }

    const uint64_t handled_access = BuildHandledFilesystemAccess(abi_version);
    const struct AcpLandlockRulesetAttr ruleset_attributes = {
        .handled_access_fs = handled_access,
        .handled_access_net = 0,
        .scoped = abi_version >= 6
            ? LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET | LANDLOCK_SCOPE_SIGNAL
            : 0,
    };
    const int ruleset_file_descriptor = LandlockCreateRuleset(
        &ruleset_attributes,
        sizeof(ruleset_attributes),
        0
    );
    if (ruleset_file_descriptor < 0) {
        return false;
    }

    /*
     * profile root は一度だけ symlink 拒否で開き、同じ inode FD を Landlock rule と
     * 適用後の再帰検査で共有する。path すり替えで検査対象と許可対象を分離させない。
     */
    const int profile_file_descriptor = OpenDirectoryWithoutSymlinks(arguments->profile);
    if (profile_file_descriptor < 0) {
        close(ruleset_file_descriptor);
        return false;
    }
    struct stat profile_stat;
    if (
        fstat(profile_file_descriptor, &profile_stat) != 0 ||
        S_ISDIR(profile_stat.st_mode) == 0
    ) {
        close(profile_file_descriptor);
        close(ruleset_file_descriptor);
        return false;
    }

    bool rules_added = AddFixedRuntimeRules(ruleset_file_descriptor, handled_access);
    if (rules_added) {
        rules_added = AddPathRuleFromDescriptor(
            ruleset_file_descriptor,
            profile_file_descriptor,
            ACP_PROFILE_ACCESS,
            handled_access,
            ACP_PATH_DIRECTORY
        );
    }
    if (rules_added) {
        rules_added = AddPathRule(
            ruleset_file_descriptor,
            arguments->working_directory,
            ACP_READ_ACCESS,
            handled_access,
            ACP_PATH_DIRECTORY,
            true,
            true
        );
    }
    for (
        size_t index = 0;
        rules_added && index < arguments->read_file_count;
        index++
    ) {
        rules_added = AddPathRule(
            ruleset_file_descriptor,
            arguments->read_files[index],
            LANDLOCK_ACCESS_FS_READ_FILE,
            handled_access,
            ACP_PATH_REGULAR_FILE,
            true,
            true
        );
    }
    if (
        rules_added == false ||
        prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 ||
        LandlockRestrictSelf(ruleset_file_descriptor, 0) != 0
    ) {
        close(profile_file_descriptor);
        close(ruleset_file_descriptor);
        return false;
    }
    close(ruleset_file_descriptor);

    /*
     * policy 適用後、provider を exec する直前に profile inode tree を検査する。
     * 失敗しても隔離なしで再実行せず、固定 setup error で終了する。
     */
    const bool profile_valid = ValidateProfileDirectory(
        profile_file_descriptor,
        profile_stat.st_dev
    );
    close(profile_file_descriptor);
    return profile_valid;
}

int main(int argument_count, char **argument_values) {
    struct AcpArguments arguments;
    if (ParseArguments(argument_count, argument_values, &arguments) == false) {
        fputs("ACP sandbox arguments are invalid.\n", stderr);
        return 126;
    }
    if (ApplySandbox(&arguments) == false) {
        fputs("ACP sandbox setup failed.\n", stderr);
        return 126;
    }

    execv(arguments.command_argv[0], arguments.command_argv);
    fputs("ACP sandbox command execution failed.\n", stderr);
    return 126;
}
