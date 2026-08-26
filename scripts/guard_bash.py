"""PreToolUse 守卫：拦截触碰受保护面的写入 / 执行操作。

判定分两步 —— 先规范化（变量回填、shlex 分词、路径归一），再看受保护路径
处在什么位置：只有写入 / 执行位置才拦，读取位置按 READ_OK 白名单放行。
契约文件允许 Read（模型需要读它们写代码），守卫脚本与本配置不允许 Read。

失败一律按拒绝处理：命令解析不了、未知命令带受保护路径参数、守卫自身
抛异常，全部 exit(2)，不给静默放行留口子。
"""
import fnmatch, json, os, posixpath, re, shlex, sys

# ------------------------------------------------------------------ 保护面

PROT_PATHS = [
    "maos/contracts/events.py",
    "maos/contracts/states.py",
    ".contracts.lock",
    "scripts/guard_bash.py",
    "scripts/relock_contracts.py",
    ".claude/settings.json",
    ".claude/settings.local.json",
]
# 契约文件允许读，其余受保护路径连读都不许
READ_OK = {"maos/contracts/events.py", "maos/contracts/states.py"}
# 不会重名的 basename，覆盖 `cd scripts && python3 relock_contracts.py` 这类相对调用。
# events.py / states.py 是通用名，故意不收，免得误伤别处的同名文件。
BARE_MATCH = {"guard_bash.py", "relock_contracts.py", ".contracts.lock"}

# 只读命令白名单。白名单外的一律按写入位置处理（fail-closed）。
READ_SAFE = {
    "echo", "printf", "cat", "head", "tail", "less", "more", "nl", "od",
    "grep", "egrep", "fgrep", "rg", "ag", "wc", "ls", "find", "file", "stat",
    "diff", "cmp", "sort", "uniq", "cut", "tr", "tree", "pwd", "which", "type",
    "basename", "dirname", "column", "jq", "yq", "shasum", "sha256sum", "md5",
    "pytest", "true", "false", "test", "date", "sleep", "cd", "export", "set",
}
# 只是前缀包装，真正的程序名在后面
WRAPPERS = {"env", "sudo", "nohup", "time", "command", "builtin", "exec",
            "xargs", "stdbuf", "nice", "then", "do", "else", "!"}
INTERPRETERS = {"python", "python3", "perl", "ruby", "node", "sh", "bash", "zsh", "php"}
GIT_READ = {"diff", "status", "log", "show", "grep", "ls-files", "blame",
            "describe", "rev-parse", "cat-file", "shortlog"}

# 无法做位置判定的构造 —— 命中就对整条命令做规范化子串扫描
OPAQUE_RE = re.compile(r"\$\(|`|\beval\b|\bbase64\b|\bawk\b|\bsource\b|<<<")
# 授权变量只许读（echo $MAOS_RELOCK），不许在命令里赋值 / 导出 / 清除
RELOCK_WRITE_RE = re.compile(
    r"MAOS_RELOCK\s*=|(?:^|[\s;&|])(?:export|unset|declare|typeset)\s+MAOS_RELOCK\b")
ASSIGN_RE = re.compile(r"""(?:^|[\s;&|(])([A-Za-z_]\w*)=("[^"]*"|'[^']*'|[^\s;&|)]*)""")
VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")
WRITE_REDIR_RE = re.compile(r"^[0-9&]*>>?\|?$")
READ_REDIR_RE = re.compile(r"^[0-9]*<<?<?$")
SEP_CHARS = set(";|&()")


class Blocked(Exception):
    def __init__(self, path, where):
        super().__init__(path)
        self.path = path
        self.where = where


# -------------------------------------------------------------- 路径与位置

def norm_path(tok):
    """归一到可比较的形式：展开 ~、折叠 ./ // ..，保留通配符。"""
    t = (tok or "").strip()
    if not t:
        return ""
    if t.startswith("~"):
        t = t[1:].lstrip("/")
    if not t:
        return ""
    return posixpath.normpath(t)


def has_glob(t):
    return any(c in t for c in "*?[")


def hits(token, write_pos):
    """token 命中哪个受保护路径；未命中返回 None。"""
    t = norm_path(token)
    if t in ("", ".", "..", "/"):
        return None
    glob = has_glob(t)
    for p in PROT_PATHS:
        if t == p or t.endswith("/" + p):
            return p
        base = posixpath.basename(p)
        if base in BARE_MATCH and t == base:
            return p
        # 通配符可能展开到受保护路径 —— 只在写位置判，否则
        # Glob(pattern="**/*.py") 这类正常操作会被整个拦掉。
        if write_pos and glob and fnmatch.fnmatch(p, t):
            return p
    return None


def check_token(token, write_pos):
    p = hits(token, write_pos)
    if p is None:
        return
    if not write_pos and p in READ_OK:
        return
    raise Blocked(p, "写入/执行位置" if write_pos else "读取位置")


def scan_opaque(text, where="不透明载荷"):
    """无法解析的片段：去掉引号与反斜杠后做子串扫描，命中即拦。"""
    squashed = re.sub(r"""['"\\]""", "", text or "")
    for p in PROT_PATHS:
        if p in squashed:
            raise Blocked(p, where)
        base = posixpath.basename(p)
        if base in BARE_MATCH and base in squashed:
            raise Blocked(p, where)


# ------------------------------------------------------------------ Bash

def substitute_vars(cmd):
    """把同一条命令里定义的 NAME=value 回填到 $NAME / ${NAME}。"""
    env = {}
    for m in ASSIGN_RE.finditer(cmd):
        val = m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        env[m.group(1)] = val
    if not env:
        return cmd

    def repl(m):
        return env.get(m.group(1) or m.group(2), m.group(0))

    out = cmd
    for _ in range(3):
        new = VAR_RE.sub(repl, out)
        if new == out:
            break
        out = new
    return out


def tokenize(line):
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""          # # 不当注释，免得把路径藏在井号后面
    try:
        return list(lex)
    except ValueError as exc:
        raise Blocked("<命令无法解析: %s>" % exc, "解析失败")


def split_segments(tokens):
    """按 ; | & && || ( ) 切成若干 simple command。"""
    segs, cur = [], []
    for t in tokens:
        if t and set(t) <= SEP_CHARS:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def check_segment(argv):
    # 1. 重定向：> 之后是写位置，< 之后是读位置
    rest, i = [], 0
    while i < len(argv):
        t = argv[i]
        if WRITE_REDIR_RE.match(t) or READ_REDIR_RE.match(t):
            if i + 1 < len(argv):
                check_token(argv[i + 1], bool(WRITE_REDIR_RE.match(t)))
                i += 2
                continue
            i += 1
            continue
        rest.append(t)
        i += 1
    if not rest:
        return

    # 2. 剥掉前置赋值与包装器，拿到真正的程序名
    while rest and (re.fullmatch(r"[A-Za-z_]\w*=.*", rest[0], re.S)
                    or posixpath.basename(rest[0]) in WRAPPERS):
        rest = rest[1:]
    if not rest:
        return

    prog = posixpath.basename(rest[0])
    args = rest[1:]

    # 3. 定位置
    if prog == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        write_pos = sub not in GIT_READ
    elif prog in INTERPRETERS:
        if "-c" in args or "-e" in args:
            scan_opaque(" ".join(args), "解释器内联代码")
        # 其余参数按「被执行」处理：python3 scripts/relock_contracts.py 要拦
        write_pos = True
    elif prog == "sed":
        write_pos = any(a.startswith("-i") for a in args)
    elif prog in READ_SAFE:
        write_pos = False
    else:
        write_pos = True                     # 未知命令 → fail-closed

    for a in args:
        check_token(a, write_pos)
    check_token(rest[0], True)               # 程序名本身也可能是受保护路径


def check_bash(command):
    raw = command or ""
    if RELOCK_WRITE_RE.search(raw):
        raise Blocked("MAOS_RELOCK", "授权变量赋值")
    if OPAQUE_RE.search(raw):
        scan_opaque(raw)
    for line in substitute_vars(raw.replace("\\\n", " ")).split("\n"):
        if line.strip():
            for seg in split_segments(tokenize(line)):
                check_segment(seg)


# ------------------------------------------------------------------ 分派

def main():
    payload = json.load(sys.stdin)
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}

    if tool == "Bash":
        check_bash(ti.get("command", ""))
    elif tool == "Read":
        check_token(ti.get("file_path", ""), False)
    elif tool in ("Edit", "Write"):
        check_token(ti.get("file_path", ""), True)      # content 一律不看
    elif tool == "NotebookEdit":
        check_token(ti.get("notebook_path", ""), True)
    elif tool == "Grep":
        for f in ("path", "glob"):                      # pattern 一律不看
            check_token(ti.get(f) or "", False)
    elif tool == "Glob":
        for f in ("path", "pattern"):
            check_token(ti.get(f) or "", False)
    # 其余工具不在 matcher 内，放行


if __name__ == "__main__":
    if os.environ.get("MAOS_RELOCK") == "1":
        sys.exit(0)
    try:
        main()
    except Blocked as b:
        print("blocked: 该操作触碰受保护面 %s（%s）。停止当前工作并向人类报告。"
              % (b.path, b.where), file=sys.stderr)
        sys.exit(2)
    except SystemExit:
        raise
    except BaseException as exc:
        print("guard internal error: %s: %s，按拒绝处理。"
              % (type(exc).__name__, exc), file=sys.stderr)
        sys.exit(2)
    sys.exit(0)
