"""Pure rules behind the run permission mode and the built-in blocklist."""
import re
from types import SimpleNamespace

import pytest
import sqlglot

from app.services import tool_blocklist
from app.services import tool_permission_mode as pm
from app.services.tool_blocklist import (
    REASON_DELETE as DELETE,
    REASON_DROP as DROP,
    REASON_TRUNCATE as TRUNCATE,
    REASON_UNREADABLE as UNREADABLE,
    block_reason,
    shell_block_reason,
    sql_block_reason,
)

# sqlglot's default dialect plus the four the blocklist reads with.
FIVE_DIALECTS = ("", "tsql", "postgres", "mysql", "snowflake")


@pytest.fixture
def sql_reads(monkeypatch):
    """Counts the work a check hands to sqlglot: the calls and bytes given to
    its tokenizer and to the SQL guard's parser. Both spies delegate, so the
    check behaves as without them. Work counts replace wall-clock bounds
    (tests/AGENTS.md: no timing asserts)."""
    reads = SimpleNamespace(tokenize_calls=0, tokenize_bytes=0, parse_calls=0, parse_bytes=0)
    real_tokenize, real_parse = sqlglot.tokenize, tool_blocklist.parse_guard_sql

    def tokenize(sql, *args, **kwargs):
        reads.tokenize_calls += 1
        reads.tokenize_bytes += len(sql.encode("utf-8", "surrogatepass"))
        return real_tokenize(sql, *args, **kwargs)

    def parse(sql, *args, **kwargs):
        reads.parse_calls += 1
        reads.parse_bytes += len(sql.encode("utf-8", "surrogatepass"))
        return real_parse(sql, *args, **kwargs)

    monkeypatch.setattr(tool_blocklist.sqlglot, "tokenize", tokenize)
    monkeypatch.setattr(tool_blocklist, "parse_guard_sql", parse)
    return reads


# A check reads its text a bounded number of times in each of the five
# dialects: the text itself, and at most one blanked copy (MySQL /*! bodies).
_READS_PER_DIALECT = 2


def _every_tokenizer_raises(text):
    for d in FIVE_DIALECTS:
        try:
            sqlglot.tokenize(text, read=d)
        except Exception:
            continue
        return False
    return True


# ── blocklist ───────────────────────────────────────────────────────────────

_DESTRUCTIVE_SHELL = [
    "rm -rf /", "rm -fr ~", "sudo rm -rf /*", "rm --recursive --force $HOME",
    "ls && rm -rf /", "cd /tmp; rm -Rf *", "rm --no-preserve-root -rf /",
    "mkfs.ext4 /dev/sdb1", "dd if=/dev/zero of=/dev/sda bs=1M", "shutdown -h now",
    "sudo reboot", ":(){ :|:& };:", "echo x > /dev/sda",
    "Format-Volume -DriveLetter D", "Remove-Item -Recurse -Force C:\\",
    "systemctl reboot", "/sbin/shutdown -r now", "sudo -n reboot", "sh -c 'rm -rf /'",
    "bash -lc \"cd / && rm -rf *\"", "echo `rm -rf ~`", "x=$(rm -rf /)",
    # Wrapper options that take a value, and trailing slashes.
    "sudo -u root rm -rf /", "timeout 10 rm -rf /", "nice -n 5 rm -rf /", "env X=1 rm -rf /",
    "sudo -u postgres timeout 60s rm -fr /var/", "rm -rf /etc/", "rm -rf /home/", "rm -rf ${HOME}/",
    "find / -print0 | xargs -0 rm -rf /", "sudo -u root shutdown -h now", "timeout 5 reboot",
    # Wildcards under protected dirs; nested shells behind flags; su -c.
    "rm -rf /home/*", "rm -rf /var/*", "rm -rf ~/*/", "bash -l -c \"sudo reboot\"", "sh -x -c 'reboot'",
    "sudo --user root reboot", "sudo --user=root reboot", "su -c \"shutdown -h now\"", "su root -c 'rm -rf /'",
    "init 0", "sudo init 6", "telinit 0",
    # SQL through a database client.
    "sudo -u postgres psql -c \"DROP DATABASE analytics\"", "psql -c \"DROP TABLE orders\"",
    "mysql -e \"DROP DATABASE shop\"", "psql -c \"TRUNCATE orders\"", "echo \"DROP TABLE x;\" | psql",
    "psql --command=\"DELETE FROM orders\"", "sqlcmd -Q \"DELETE orders\"",
]


@pytest.mark.parametrize("command", _DESTRUCTIVE_SHELL)
def test_destructive_shell_commands_are_blocked(command):
    assert shell_block_reason(command)


@pytest.mark.parametrize("command", [
    "ls -la", "rm -rf ./build", "rm -rf /tmp/run-123", "rm old.log", "cat /etc/hostname",
    "df -h", "dd if=a.img of=b.img",
    "dd if=/dev/sda of=/dev/null bs=1M count=1", "Get-Date -Format H:mm", "systemctl status nginx",
    "sudo -u postgres psql -c 'select 1'", "timeout 10 ls /", "rm -rf /tmp/build/",
    "rm -rf ./build/*", "rm -rf /var/tmp/cache/*", "su - postgres -c 'psql -l'", "bash -lc 'ls /home'",
    "psql -c \"select count(*) from orders\"", "grep -r 'drop table' /var/log/app.log",
    "psql -c \"DELETE FROM orders WHERE id = 3\"",
    # Near misses of the rules below.
    "rm -rf ../build", "psql -c\"select 1\"", "shred secret.txt", "cp a b", "chmod -R 755 ./build",
    "find . -name x -delete", "git log --format c:%H", "systemctl status nginx --no-pager",
])
def test_ordinary_shell_commands_are_not_blocked(command):
    assert shell_block_reason(command) is None


_SQL_ATTACHED = [
    # SQL attached to a short option, alone or after other options in a cluster.
    'psql -c"drop table x"', 'mysql -e"DROP DATABASE x"', 'sqlcmd -Q"DROP DATABASE x"',
    'psql -X -c"drop table x"', 'psql -U u -c"drop table x"', 'psql -Xc"drop table x"',
    'duckdb -c"drop table x"', 'clickhouse-client -q"DROP DATABASE x"', 'snowsql -q"drop database x"',
]


@pytest.mark.parametrize("command", _SQL_ATTACHED)
def test_sql_attached_to_a_db_client_option_is_refused(command):
    assert shell_block_reason(command) == DROP


@pytest.mark.parametrize("target", [
    "/etc/.", "/bin/..", "~/.", "/home/user/..", "/./", "../..", "//etc", "/usr/./", "./..",
    "/tmp/x/../../etc", "$HOME/.", "/sbin", "/lib64/", "~/../..",
])
def test_a_dotted_path_to_a_protected_root_is_refused(target):
    assert shell_block_reason(f"rm -rf {target}") == "recursive forced delete of a root, home or wildcard path"


_DISK_AND_ROOT = [
    # Block devices written by disk tools, copies and tee.
    "wipefs -a /dev/sda", "blkdiscard /dev/nvme0n1", "shred -n1 /dev/sdb", "sgdisk --zap-all /dev/sda",
    "sfdisk /dev/vda < layout", "parted -s /dev/xvda mklabel gpt", "fdisk /dev/mmcblk0",
    "cp zero.img /dev/sda", "cat x | tee /dev/sdb", "truncate -s 0 /dev/sdc", "dd if=x of=/dev/mmcblk0p1",
    # Deletes and ownership changes on a protected root.
    "find / -name '*.log' -delete", "find /etc -type f -exec rm -f {} +", "find ~ -delete",
    "chmod -R 777 /", "chown -R nobody /etc", "sudo chmod --recursive 000 /usr/.", "chgrp -R x /home",
    # A format behind cmd /c.
    "cmd /c format c:", "cmd.exe /c \"format d: /q\"",
]


@pytest.mark.parametrize("command", _DISK_AND_ROOT)
def test_disk_wipes_and_root_changes_are_refused(command):
    assert shell_block_reason(command) == "destructive system command"


_SHUTDOWN_WORDS = [
    # Shutdown words anywhere in the command, behind any program.
    "ssh host shutdown -h now", "echo shutdown | sh", "ssh host 'sudo reboot'", "halt", "poweroff",
    "docker exec box /sbin/init 0", "systemctl poweroff", "Stop-Computer -Force", "Restart-Computer",
    "SHUTDOWN /s /t 0", "watch -n 60 reboot",
]


@pytest.mark.parametrize("command", _SHUTDOWN_WORDS)
def test_shutdown_words_are_refused_behind_any_program(command):
    assert shell_block_reason(command) == "shuts down or restarts the server"


@pytest.mark.parametrize("command", [
    # Accepted false refusals (named in the PR): the shutdown and mkfs words
    # are matched as text anywhere in a command, like the other destructive
    # commands, so a command that only mentions one is refused too.
    "systemctl status reboot.target", "grep mkfs /etc/fstab", "man mkfs", "echo shutdown",
    "env X=1 echo reboot", "sudo grep reboot /var/log", "psql -c \"select 1 where state='shutdown'\"",
    "find ~ -name '*.tmp' -delete", "find /home -name core -delete",
])
def test_accepted_false_refusals_of_shell_text(command):
    assert shell_block_reason(command)


# ── blocklist: shell forms of this round ────────────────────────────────────

_RM_ROOT = "recursive forced delete of a root, home or wildcard path"
_DESTRUCTIVE = "destructive system command"
_POWER = "shuts down or restarts the server"


@pytest.mark.parametrize("command,reason", [
    # bash reads an unbalanced quote inside a # comment; shlex does not.
    ("rm -rf \"/var\" # it's", _RM_ROOT), ("rm -rf \"$HOME/\" # clean the user's home", _RM_ROOT),
    ("chmod -R 777 \"/etc\" # it's fine", _DESTRUCTIVE), ("find \"/var\" -delete # don't", _DESTRUCTIVE),
    ("bash -c \"rm -rf /etc\" # it's", _RM_ROOT), ("rm -rf /etc/\"\" # x'", _RM_ROOT),
])
def test_an_unbalanced_quote_in_a_comment_never_hides_a_command(command, reason):
    assert shell_block_reason(command) == reason


@pytest.mark.parametrize("command", _DESTRUCTIVE_SHELL + _SQL_ATTACHED + _DISK_AND_ROOT + _SHUTDOWN_WORDS)
def test_every_refused_command_is_still_refused_behind_an_unbalanced_comment(command):
    assert shell_block_reason(command + " # it's")


@pytest.mark.parametrize("command", [
    # find: options before the roots, and a program in front of rm in -exec.
    "find -P / -delete", "find -L /etc -name x -exec rm {} +", "find -- / -delete", "find -H /etc -exec rm {} +",
    "find -D tree / -delete", "find / -exec sudo rm -f {} +", "find -O3 /var -delete",
    # cp / install onto a disk device: redirects, trailing options, comments, -t.
    "cp image.iso /dev/sdb 2>/dev/null", "cp image.iso /dev/sdb -v", "cp image.iso /dev/disk/by-id/ata-X",
    "cp disk.img /dev/sda 2>&1", "cp disk.img /dev/sda > log", "cp disk.img /dev/sda # flash",
    "sudo cp -v disk.img /dev/nvme0n1 2>/dev/null", "cp -t /dev/sda x", "install x /dev/sda",
    "cp --target-directory=/dev/sda x",
    # More disk devices and formatting tools.
    "wipefs -a /dev/mapper/vg-root", "mke2fs /dev/sdb1", "mkswap /dev/sda2", "blkdiscard /dev/dm-0",
    "shred /dev/md0", "wipefs -a /dev/loop0", "mkfs.xfs /dev/mapper/data",
])
def test_find_cp_and_disk_device_forms_are_refused(command):
    assert shell_block_reason(command) == _DESTRUCTIVE


@pytest.mark.parametrize("command", [
    "cp /dev/sda disk.img", "cp a b 2>/dev/null", "find . -name x -delete", "find -L . -name x -delete",
    "cp -v a.txt /tmp/b.txt", "install -m 644 a.conf /etc/app/a.conf", "cp /dev/sda img; ls /dev/sdb",
    "dd if=/dev/sda of=disk.img", "cat /dev/sda | tee disk.img",
])
def test_find_and_cp_near_misses_pass(command):
    assert shell_block_reason(command) is None


@pytest.mark.parametrize("command,reason", [
    ('SQLCMD -Q "drop database x"', DROP), ('sqlcmd.exe -Q "drop database x"', DROP),
    ('psql.exe -c "drop table x"', DROP), ('C:\\tools\\psql.exe -c "drop table x"', DROP),
    ('MYSQL.EXE -e "DROP DATABASE x"', DROP),
])
def test_windows_db_client_names_are_read(command, reason):
    assert shell_block_reason(command) == reason


@pytest.mark.parametrize("command", [
    "rm -rf ~root", "rm -rf ~alice", "rm -rf ~alice/", "rm -rf \"$DIR/\"*", "rm -rf $DIR/*", "rm -rf ${DIR}/*",
    "rm -rf \"${BUILD_ROOT}\"/*",
])
def test_rm_of_another_home_or_a_variable_wildcard_is_refused(command):
    # A variable wildcard: an unset variable expands to /* (an accepted false
    # refusal for a set one, named in the PR).
    assert shell_block_reason(command) == _RM_ROOT


@pytest.mark.parametrize("command", ["rm -rf ~alice/tmp", "rm -rf $DIR/build", "rm -rf ${DIR}/cache"])
def test_rm_below_another_home_or_a_variable_passes(command):
    assert shell_block_reason(command) is None


@pytest.mark.parametrize("command", [
    "last reboot", "grep -r reboot /var/log", "tail shutdown.log", "zgrep -i halt /var/log/syslog.gz",
    "journalctl -u reboot-notifier", "/usr/bin/grep shutdown notes.txt", "fdisk -l /dev/sda",
    "sfdisk -l /dev/sda", "fdisk --list /dev/nvme0n1",
])
def test_read_only_commands_that_mention_a_shutdown_word_or_list_a_disk_pass(command):
    assert shell_block_reason(command) is None


@pytest.mark.parametrize("command,reason", [
    ("grep reboot f | sh", _POWER), ("last reboot; reboot", _POWER), ("grep x $(reboot)", _POWER),
    ("cat `reboot`", _POWER), ("tail -f log && shutdown now", _POWER), ("grep reboot\nreboot", _POWER),
    ("fdisk /dev/sda", _DESTRUCTIVE), ("fdisk -l /dev/sda --wipe always", _DESTRUCTIVE),
    ("sfdisk /dev/sda < layout", _DESTRUCTIVE),
])
def test_the_read_only_exemptions_stay_narrow(command, reason):
    assert shell_block_reason(command) == reason


@pytest.mark.parametrize("command", [
    "ls ; cp a /dev/sda", "ls && wipefs -a /dev/sda", "true | dd of=/dev/sda", "ls a b c ; ls ; tee /dev/sda",
])
def test_a_program_with_nothing_to_its_right_never_hides_a_later_one(command):
    # A first program with no device before the separator must not end the scan.
    assert shell_block_reason(command) == _DESTRUCTIVE


# ── blocklist: one reading of a shell command ───────────────────────────────

_EVERY_REFUSED_COMMAND = _DESTRUCTIVE_SHELL + _SQL_ATTACHED + _DISK_AND_ROOT + _SHUTDOWN_WORDS

_SUBSTITUTION_BESIDE_A_DISK = [
    "dd if=img bs=$(cat bs) of=/dev/sdb", "dd bs=$(blockdev --getss /dev/sdb) if=img of=/dev/sdb",
    "truncate -s $(stat -c %s img) /dev/sdb", "shred -n $(nproc) /dev/sdb", "wipefs -a $(echo -f) /dev/sdb",
    "sudo wipefs --all $(realpath /dev/disk/by-id/usb-X)", "sgdisk --zap-all $(realpath /dev/sdb)",
    "parted $(echo /dev/sda) mklabel gpt", "cp img $(echo /dev/sda)", "cp $(ls img*) /dev/sda",
    "tee -a $(echo log) /dev/sda < img", "echo x | tee >(cat) /dev/sda",
    # The same behind backticks and a subshell.
    "dd if=img bs=`cat bs` of=/dev/sdb", "shred -n `nproc` /dev/sdb", "cp `ls img*` /dev/sda",
    "(cd /tmp && cp img /dev/sda)", "wipefs -a /dev/sdb $(true)",
]


@pytest.mark.parametrize("command", _SUBSTITUTION_BESIDE_A_DISK)
def test_a_substitution_beside_a_disk_write_never_hides_it(command):
    # ( ) are not statement separators: the tool and its device stay one segment.
    assert shell_block_reason(command) == _DESTRUCTIVE


_AROUND_A_SUBSTITUTION = ["x=$({})", "({})", "echo `{}`", "{} $(echo x)", "{} `echo x`", "echo $(true); {}",
                          "( true ) && {}"]


@pytest.mark.parametrize("shape,command", [
    (shape, command) for shape in _AROUND_A_SUBSTITUTION for command in _EVERY_REFUSED_COMMAND
    # Backticks do not nest: a command with its own would end the outer pair.
    if not ("`" in shape and "`" in command)
])
def test_every_refused_command_is_still_refused_around_a_substitution(shape, command):
    assert shell_block_reason(shape.replace("{}", command))


# Every line break a shell may end a statement at: bash at \n; PowerShell (the
# shell of a Windows remote server) also at a lone \r; the rest are the other
# Unicode line boundaries.
_LINE_BREAKS = ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]


@pytest.mark.parametrize("command", [
    "cat x\rStop-Computer", "ls\rRestart-Computer", "grep a b\rStop-Computer -Force", "cat x\rshutdown",
    # Any character that is not printable ends the read-only reading too.
    "cat x\x1bStop-Computer", "tail x reboot",
])
def test_a_line_break_never_gets_a_shutdown_past_the_read_only_exemption(command):
    assert shell_block_reason(command) == _POWER


@pytest.mark.parametrize("line_break", _LINE_BREAKS)
@pytest.mark.parametrize("command", _EVERY_REFUSED_COMMAND)
def test_every_refused_command_is_still_refused_after_any_line_break(line_break, command):
    assert shell_block_reason("cat x" + line_break + command)
    assert shell_block_reason(command + line_break + "cat x")


@pytest.mark.parametrize("command", ['less "+!reboot" /etc/hosts', "less +!poweroff x", "less shutdown.log"])
def test_a_pager_is_not_a_read_only_command(command):
    # less runs a command from its arguments (+!cmd): no exemption for it.
    assert shell_block_reason(command) == _POWER


@pytest.mark.parametrize("command", [
    # A quoted or escaped separator is a word, not the end of the command.
    'tee "|" /dev/sda', 'shred ";" /dev/sda', 'wipefs -a "&" /dev/sda', "tee '|' /dev/sda", "tee \\| /dev/sda",
    'cp x ";" /dev/sda', 'dd if=x ";" of=/dev/sda', 'tee "&&" /dev/sda', 'shred "\n" /dev/sda',
    # Other spellings of the device path.
    "dd if=x of=//dev/sda", "dd if=x of=/./dev/sda", "cp x //dev/sda", "tee /dev/./sda", "cat x | tee /dev//sda",
    "dd if=x of=/tmp/../dev/sda", "wipefs -a /dev/shm/../sda", "cp x /dev/fd/../sda", "echo x > //dev/sda",
    "echo x > /dev/./sda", "echo x > /dev/pts/../sda", 'echo x > "/dev/sda"', "echo x >'/dev/sda'",
    # Redirect operators.
    "cat img >| /dev/sda", "cat img >|/dev/sda", "cat img >>/dev/sda", "echo x>/dev/sda", "cat img &> /dev/sda",
    "cat img 1> /dev/sda", "cat img &>>/dev/sda", "exec 3<>/dev/sda", "cat img >& /dev/sda",
    # A redirect ends the word before it: dd<x is dd.
    "dd<x of=/dev/sda", "tee</dev/null /dev/sda", "sh<<<'dd if=x of=/dev/sda'", "dd<x>/dev/sda",
    "cp x>/dev/null /dev/sda", "shred</dev/null /dev/sda",
    # Any device node that is not a harmless one, by glob too.
    "wipefs -a /dev/vg0/root", "dd if=x of=/dev/nbd0", "cp img /dev/sd?", "cat img > /dev/zd0", "shred /dev/s*",
    "cat img > /dev/block/8:0", "tee /dev/[s]da < img", "cp img /d?v/sda", "cp img /dev/{sda,sdb}",
])
def test_a_disk_write_is_refused_however_its_words_are_spelled(command):
    assert shell_block_reason(command) == _DESTRUCTIVE


@pytest.mark.parametrize("command", [
    # $IFS is a space; an empty positional parameter is nothing.
    "rm${IFS}-rf${IFS}/etc", "rm$IFS-rf$IFS/etc", "rm$IFS$9-rf$IFS$9/etc", "rm -rf${IFS}/",
    # Brace lists, $'…' quoting and globs that name a protected root.
    "{rm,-rf,/etc}", "rm -rf /{etc,var}", "rm -rf /{tmp/x,etc}", "rm -rf $'/etc'", "$'\\x72\\x6d' -rf /",
    "rm -rf $'\\057etc'", "rm -rf /et?", "rm -rf /[e]tc", "rm -rf /h*", "rm -rf /va[r]/*",
    # The working directory by its variable; a redirect glued to a word.
    "rm -rf $PWD", "rm -rf ${PWD}/", 'rm -rf "$PWD/.."', "rm -rf />log", "rm<x -rf /",
    # A quote in a comment or a here-document never hides the next line.
    "# it's\nrm -rf / # it's", "cat <<EOF\nit's\nEOF\nrm -rf / # '", "# don't\nrm -rf /etc\n# don't",
    # A body nested in more parentheses or quotes is still read.
    'echo "$(echo "a"; rm -rf /; (true))"', 'echo "$(echo $(echo "x"; rm -rf /))"', "echo $( (rm -rf /) )",
    "cat <(rm -rf /)",
])
def test_a_root_delete_is_refused_however_its_words_are_spelled(command):
    assert shell_block_reason(command) == _RM_ROOT


@pytest.mark.parametrize("command", [
    # Harmless device nodes and redirects.
    "ls 2>/dev/null", "make > /dev/null 2>&1", "make &>/dev/null", "echo x | tee /dev/stderr", "cp a /dev/shm/b",
    "echo hi > /dev/tty", "cat a >| b", "echo x >> /dev/stdout", "ls /dev/sda", "lsblk /dev/sda",
    "dd if=/dev/vg0/root of=img", "cp /dev/nbd0 img", "cat /dev/vg0/root | gzip > img.gz", "head -c 16 /dev/urandom",
    "dd if=/dev/urandom of=/tmp/x bs=1 count=8", "echo x > /dev/fd/2", "cat x > /tmp/dev/sda",
    # Quoted separators and braces beside nothing protected.
    'echo "a;b" c', 'tee "|" log', "rm -rf /tmp/{a,b}", "awk '{print $1,$2}' f", "echo {a,b}",
    # Globs and quoting that name no protected root.
    "rm -rf ./*.pyc", "rm -rf /tmp/x*", "rm -rf $'/tmp/x'", "rm -rf /tmp/[ab]", "rm -rf build/*",
    "rm -rf $PWD/build", "rm -rf ${PWD}/dist/", "find $PWD -name '*.pyc' -delete", "find ./ -name x -delete",
    "wc -l<f", "ls>out.txt", "sort<a>b", "cat x 2>&1 >/dev/null",
    # Nested parentheses are not nested shells.
    "python3 -c \"print(len(str(int(float('1')))))\"", "echo $((1+(2*(3+(4)))))", "echo $(echo $(echo $(echo $(ls))))",
    # Several lines, comments with quotes.
    "# it's fine\nls -la # isn't it", "echo a\r\necho b", "cat <<EOF\nit's\nEOF\nls",
])
def test_ordinary_commands_near_the_new_readings_pass(command):
    assert shell_block_reason(command) is None


@pytest.mark.parametrize("command,reason", [
    # A quoted separator beside Remove-Item is one more path to remove.
    ('Remove-Item ";" -Recurse -Force C:\\', _DESTRUCTIVE), ("Remove-Item '|' -Recurse -Force C:\\", _DESTRUCTIVE),
    ("(Remove-Item -Recurse -Force C:\\)", _DESTRUCTIVE),
    # Options between init and the runlevel.
    ("telinit --no-wall 0", _POWER), ("sudo init --no-wall 6", _POWER), ("ssh host telinit -t 5 0", _POWER),
    # A substitution in front of find's roots.
    ("find $(echo -L) / -delete", _DESTRUCTIVE), ("find `echo -H` /etc -exec rm {} +", _DESTRUCTIVE),
    ("find $(echo -L) $(echo -O2) /var -delete", _DESTRUCTIVE),
    # The root directory by its /proc name.
    ("rm -rf /proc/self/root/etc", _RM_ROOT), ("rm -rf /proc/1/root/", _RM_ROOT),
    ("dd if=x of=/proc/1/root/dev/sda", _DESTRUCTIVE), ("cat x > /proc/self/root/dev/sda", _DESTRUCTIVE),
    ("chmod -R 777 /proc/self/root/proc/self/root/usr", _DESTRUCTIVE),
    # Long options cut short, as GNU tools and database clients accept them.
    ("rm --r --f /", _RM_ROOT), ("rm --recurs --forc /etc", _RM_ROOT), ("rm --no-p -r /tmp/x", _RM_ROOT),
    ("chmod --rec 777 /etc", _DESTRUCTIVE), ('psql --comm "drop table x"', DROP),
    ('mysql --exec="DROP DATABASE x"', DROP), ('clickhouse-client --que "DROP DATABASE x"', DROP),
    # The program by a name given to it in the command.
    ("x=rm; $x -rf /", _RM_ROOT), ("alias del=rm; del -rf /etc", _RM_ROOT), ("c=chmod; $c -R 777 /", _DESTRUCTIVE),
    # $HOME and $PWD with an expansion operator; a brace range.
    ('rm -rf "${HOME%/*}"', _RM_ROOT), ("rm -rf ${HOME:?}", _RM_ROOT), ("rm -rf ${PWD%/*}", _RM_ROOT),
    ("rm -rf /{e..e}tc", _RM_ROOT), ("cp img /dev/sd{a..c}", _DESTRUCTIVE),
    # A download or a copy written onto a device.
    ("curl -o /dev/sda http://host/img", _DESTRUCTIVE), ("wget -O /dev/sda http://host/img", _DESTRUCTIVE),
    ("curl -s http://host/img --output /dev/nvme0n1", _DESTRUCTIVE), ("ddrescue img /dev/sda", _DESTRUCTIVE),
    ("zcat img.gz | sponge /dev/sda", _DESTRUCTIVE),
    # PowerShell's and cmd's escape characters inside a word.
    ("Stop-Comp`uter", _POWER), ("Restart-Comp`uter -Force", _POWER), ("cmd /c shut^down /s", _POWER),
    ("Rem`ove-Item -Recurse -Force C:\\", _DESTRUCTIVE), ("For`mat-Volume -DriveLetter D", _DESTRUCTIVE),
])
def test_words_between_a_program_and_its_target_never_hide_it(command, reason):
    assert shell_block_reason(command) == reason


@pytest.mark.parametrize("command,reason", [
    # Two kinds of quote left open in a here-document pair up around the next
    # line twice over; the line is still read on its own.
    ("cat <<EOF\nit's \"q\nEOF\npsql -c \"drop table x\" # \"'", DROP),
    ("cat <<EOF\nit's \"q\nEOF\nmysql -e \"DROP DATABASE shop\" # \"'", DROP),
])
def test_a_line_is_read_on_its_own_whatever_quotes_surround_it(command, reason):
    assert shell_block_reason(command) == reason


@pytest.mark.parametrize("command", [
    # Accepted false refusals (named in the PR): a quoted command with a
    # separator in it is also read as raw words, which read on past it; dd's
    # harmless targets count only spelled plainly.
    "sh -c 'cp /dev/sda img; ls /dev/sdb'", "cp /dev/sda img; ls /dev/sdb # it's",
    "c\\p /dev/sda img; ls /dev/sdb # it's", "'cp' /dev/sda img; ls /dev/sdb # don't",
    "dd if=/dev/sda of=/dev/./null bs=1M count=1",
])
def test_accepted_false_refusals_of_the_raw_reading(command):
    assert shell_block_reason(command) == _DESTRUCTIVE


@pytest.mark.parametrize("command,reason", [
    # Accepted false refusals (named in the PR).
    # A write, or a writer's argument, on a device node off the harmless list.
    ("echo x > /dev/kmsg", _DESTRUCTIVE), ("curl -T /dev/sda http://host/", _DESTRUCTIVE),
    # rm, find and chmod read every later word, also past a separator.
    ("rm -rf build; ls /etc", _RM_ROOT), ("rm -rf dist && cd ~", _RM_ROOT),
    # A quoted text is read again with a level of quotes removed, and each line on its own.
    ('git commit -m "note: rm -rf / is blocked"', _RM_ROOT), ("echo 'a\nrm -rf /\nb'", _RM_ROOT),
    # A brace list over 64 words is read as its pieces: the / before it counts alone.
    ("rm -rf /{" + ",".join(f"a{i}" for i in range(70)) + "}", _RM_ROOT),
])
def test_accepted_false_refusals_of_the_shell_readings(command, reason):
    assert shell_block_reason(command) == reason


def test_a_long_brace_list_under_an_ordinary_directory_passes():
    assert shell_block_reason("rm -rf /tmp/{" + ",".join(f"a{i}" for i in range(70)) + "}") is None


@pytest.mark.parametrize("command", [
    "Remove-Item -Recurse -Force build; dir C:\\", "find $(pwd) -name '*.pyc' -delete", "find `pwd` -name x -delete",
    "git init", "telinit q", "git init -q repo && sleep 6", "cat /proc/self/root/etc/hostname",
    "rm -rf /proc/self/root/tmp/x", "ls /proc/1/root/dev/sda",
    "rm --verbose -rf /tmp/x", "rm --recursive --force ./build", "psql --csv -c 'select 1'",
    'psql --comm "select 1"', "x=rm; echo $x", "EDITOR=vim git commit", "rm -rf ${HOME}/build",
    "rm -rf ${HOME:-/tmp}/cache", "rm -rf /tmp/{1..3}", "echo {1..10}",
    "curl -o /dev/null http://host/x", "curl -so /tmp/x http://host/x", "wget -O - http://host/x",
    "wget -qO /dev/stdout http://host/x", "mount -o loop /dev/sda1 /mnt", "lsblk -o NAME /dev/sda",
    "ls -o /dev/sda", "echo `date` > /tmp/log", "grep '^foo' f", "kill `cat app.pid`",
])
def test_near_misses_of_the_words_between_rule_pass(command):
    assert shell_block_reason(command) is None


@pytest.mark.parametrize("command,reason", [
    # Two levels of quotes: the second shell's command is still read.
    ("eval \"sh<<<'dd if=x of=/dev/sda'\"", _DESTRUCTIVE),
    ("eval \"eval 'dd if=/dev/zero of=/dev/sda bs=1M'\"", _DESTRUCTIVE),
    ("ssh h \"eval 'wipefs -a /dev/sda'\"", _DESTRUCTIVE), ("ssh h \"bash <<< 'cp zero.img /dev/sda'\"", _DESTRUCTIVE),
    ("watch \"eval 'shred -n1 /dev/sdb'\"", _DESTRUCTIVE),
    ("ssh a ssh b \"eval 'parted -s /dev/xvda mklabel gpt'\"", _DESTRUCTIVE),
    ("ssh host \"eval 'rm -rf /'\"", _RM_ROOT), ("eval \"eval 'rm -rf /'\"", _RM_ROOT),
    ("eval \"eval 'find /etc -delete'\"", _DESTRUCTIVE), ("ssh h \"eval 'chmod -R 777 /'\"", _DESTRUCTIVE),
    ("watch \"eval 'chown -R nobody /usr'\"", _DESTRUCTIVE), ("eval \"eval 'cat img > /dev/sda'\"", _DESTRUCTIVE),
    # The command another interpreter is handed, inside its own call syntax.
    ("python3 -c \"import os; os.system('dd if=x of=/dev/sda')\"", _DESTRUCTIVE),
    ("python3 -c \"import os;os.system('wipefs -a /dev/sda')\"", _DESTRUCTIVE),
    ("perl -e 'system(\"tee /dev/sda\")'", _DESTRUCTIVE),
    ("awk 'BEGIN{system(\"truncate -s 0 /dev/sda\")}'", _DESTRUCTIVE),
    ("ruby -e 'system(\"shred /dev/sda\")'", _DESTRUCTIVE),
    ("node -e 'require(\"child_process\").execSync(\"parted /dev/sda mklabel gpt\")'", _DESTRUCTIVE),
    ("python3 -c \"import os; os.system('rm -rf /')\"", _RM_ROOT), ("perl -e 'system(\"rm -rf /\")'", _RM_ROOT),
    ("awk 'BEGIN{system(\"find / -delete\")}'", _DESTRUCTIVE),
    ("ruby -e 'system(\"chmod -R 777 /etc\")'", _DESTRUCTIVE),
    ("python3 -c \"import subprocess; subprocess.run('find /var -exec rm {} +', shell=True)\"", _DESTRUCTIVE),
    # The same inside a substitution: the words after it are not the command's.
    ("echo $(eval \"eval 'fdisk /dev/sda'\") -l", _DESTRUCTIVE),
])
def test_a_write_behind_two_levels_of_quotes_or_an_interpreter_call_is_refused(command, reason):
    assert shell_block_reason(command) == reason


@pytest.mark.parametrize("command", [
    "eval \"eval 'ls -la /dev/sda'\"", "ssh h \"eval 'cp /dev/sda img'\"", "ssh h \"eval 'rm -rf /tmp/x'\"",
    "python3 -c \"import os; os.system('ls /etc')\"", "perl -e 'print(\"hello (world)\")'",
    "awk 'BEGIN{system(\"date\")}'", "ruby -e 'system(\"fdisk -l /dev/sda\")'",
    "node -e 'require(\"child_process\").execSync(\"find . -name x -delete\")'",
    "python3 -c \"import shutil; shutil.rmtree('/tmp/build')\"", "watch \"eval 'dd if=/dev/sda of=img'\"",
])
def test_ordinary_commands_behind_two_levels_of_quotes_or_an_interpreter_call_pass(command):
    assert shell_block_reason(command) is None


@pytest.mark.parametrize("command,reason", [
    # Accepted false refusals (named in the PR): text an interpreter only
    # prints reads like the command it would run.
    ("python3 -c \"print('rm -rf /')\"", _RM_ROOT), ("perl -e 'print(\"dd if=x of=/dev/sda\")'", _DESTRUCTIVE),
])
def test_accepted_false_refusals_of_the_reading_without_quotes(command, reason):
    assert shell_block_reason(command) == reason


def _nested_three_levels(n_parts):
    import shlex

    inner = "|".join(f"a{i}\\b" for i in range(n_parts))
    command = inner
    for _ in range(3):
        command = f"sh -c {shlex.quote(command)}"
    return command


def test_a_nested_body_with_separators_is_read_once_per_segment(monkeypatch):
    # Work counts, not a clock: every unique piece of the command is read by
    # the word rules a bounded number of times, however deep it is nested and
    # however many readings (quotes, backslashes) each level has.
    n_parts = 16 * 1024 // 12
    command = _nested_three_levels(n_parts)
    while len(command.encode()) > 16 * 1024:
        n_parts -= 50
        command = _nested_three_levels(n_parts)
    calls = {"words": 0, "sql": 0}
    real_words, real_sql = tool_blocklist._token_reason, tool_blocklist._sql_reason

    def words(*a, **k):
        calls["words"] += 1
        return real_words(*a, **k)

    def sql(*a, **k):
        calls["sql"] += 1
        return real_sql(*a, **k)

    monkeypatch.setattr(tool_blocklist, "_token_reason", words)
    monkeypatch.setattr(tool_blocklist, "_sql_reason", sql)
    assert shell_block_reason(command) is None
    # Unique pieces: each of the n_parts segments in two readings (with and
    # without its backslash), and a handful of whole texts per level.
    assert calls["words"] <= 2 * n_parts + 128
    assert calls["sql"] == 0  # no database client anywhere

    calls["words"] = 0
    assert shell_block_reason(command.replace("a7\\b", "psql")) is None
    assert calls["sql"] <= 2 * n_parts + 128  # each unique word once, whatever number of texts hold it


@pytest.mark.timeout(60)
@pytest.mark.parametrize("command", [
    "$(" * 5000 + ")" * 5000, "sh -c " * 2700, "a;" * 8000, "`a`" * 5000, "{a,b}" * 3000, "$'\\x41'" * 2000,
    ">" * 16000, "> " * 8000, "x\n" * 8000, "# '\n" * 4000, "rm -rf {" + "a," * 7000 + "b}",
    "rm -rf {" + "a," * 8000, "rm -rf {" + "." * 16000, "'\n" * 8000, "(" * 8000 + ")" * 8000, "x=rm " * 3200,
])
def test_pathological_shell_text_is_checked_in_bounded_work(command):
    # Minutes if any reading were quadratic at the size cap; refused or passed, never hung.
    assert shell_block_reason(command) in (None, "could not read the command safely", _DESTRUCTIVE, _RM_ROOT)


def test_a_command_over_the_size_cap_is_unreadable_before_any_pattern(monkeypatch):
    class Spy:
        def __init__(self, pat):
            self.pat, self.calls = pat, 0

        def search(self, text, *a):
            self.calls += 1
            return self.pat.search(text, *a)

    spies = [Spy(p) for p in tool_blocklist._SHELL_PATTERNS + tool_blocklist._POWER_PATTERNS]
    monkeypatch.setattr(tool_blocklist, "_SHELL_PATTERNS", spies[:len(tool_blocklist._SHELL_PATTERNS)])
    monkeypatch.setattr(tool_blocklist, "_POWER_PATTERNS", spies[len(tool_blocklist._SHELL_PATTERNS):])
    cap = 16 * 1024
    assert shell_block_reason("ls " + "a" * (cap - 3)) is None  # control: at the cap it is read
    assert sum(s.calls for s in spies) > 0
    for s in spies:
        s.calls = 0
    assert shell_block_reason("ls " + "a" * (cap - 2)) == "could not read the command safely"
    assert shell_block_reason("ls " + "\u05d0" * (cap // 2)) == "could not read the command safely"  # UTF-8 bytes
    assert sum(s.calls for s in spies) == 0


@pytest.mark.timeout(20)
@pytest.mark.parametrize("word", ["tee ", "cp ", "dd ", "wipefs ", "install ", "fdisk "])
def test_a_repeated_tool_word_at_the_size_cap_is_checked_in_bounded_work(word):
    # The old word … target regexes were quadratic here (minutes at this size).
    command = (word * (16 * 1024 // len(word)))[:16 * 1024]
    assert shell_block_reason(command) is None
    target = " of=/dev/sda1" if word == "dd " else " /dev/sda1"
    assert shell_block_reason(command[:-16] + target) == _DESTRUCTIVE


def test_a_command_far_over_the_size_cap_is_refused_at_once():
    # Minutes with the old quadratic rules; refused before any of them now.
    assert shell_block_reason("tee " * 250_000 + "/dev/sda") == "could not read the command safely"


@pytest.mark.parametrize("word", ["rm -rf ", "rm ", "find ", "chmod -R ", "psql -c ", "psql ", "sh ", "cp -t ",
                                  "find -D "])
def test_repeated_program_words_cost_work_linear_in_the_words(monkeypatch, word):
    # Each rule reads the words to its right once, not once per program word
    # (rm rm rm …): a work count, not a clock (tests/AGENTS.md).
    calls = {"target": 0, "sql": 0}
    real_target, real_sql = tool_blocklist._protected_target, tool_blocklist._sql_reason

    def target(t):
        calls["target"] += 1
        return real_target(t)

    def sql(*a, **k):
        calls["sql"] += 1
        return real_sql(*a, **k)

    monkeypatch.setattr(tool_blocklist, "_protected_target", target)
    monkeypatch.setattr(tool_blocklist, "_sql_reason", sql)
    command = (word * (16 * 1024 // len(word)))[:16 * 1024].rsplit(" ", 1)[0]
    shell_block_reason(command)
    words = len(command.split())
    # The command and its one segment, each read once per word.
    assert calls["target"] <= 2 * words + 4
    assert calls["sql"] <= 4 * words + 4


@pytest.fixture
def shell_reads(monkeypatch):
    """Counts the work one shell check does: the characters it splits into
    words and the words its rules then read. The spies delegate, so the check
    behaves as without them. A work count, not a clock (tests/AGENTS.md)."""
    reads = SimpleNamespace(chars=0, words=0)

    def spy(name, field):
        real = getattr(tool_blocklist, name)

        def counted(first, *args, **kwargs):
            setattr(reads, field, getattr(reads, field) + len(first))
            return real(first, *args, **kwargs)

        monkeypatch.setattr(tool_blocklist, name, counted)

    spy("_lex", "chars")
    for rule in ("_token_reason", "_nested_bodies", "_db_client_sql"):
        spy(rule, "words")
    return reads


# The work of one shell check per character of the command, at most: every
# reading of every line, segment and nested body together.
_SHELL_WORK_PER_CHAR = 64
_SHELL_CAP = 16 * 1024
_RANDOM_SHELL_PIECES = {
    "quotes": list("ab ") + ["'", '"', "\\", ";", "|", "$(", ")"],
    "printable": [chr(c) for c in range(32, 127)] + ["\n"],
    "shell words": ["a ", "b|", "c;", "'d' ", '"e" ', "\\f ", "$(g) ", "`h` ", "(i) ", "x=1 ", "# j\n", "k\n",
                    "sh -c ", "eval "],
}


def _random_shell_text(pieces, seed):
    import random

    rng = random.Random(seed)
    out, size = [], 0
    while size < _SHELL_CAP:
        out.append(rng.choice(pieces))
        size += len(out[-1])
    text = "".join(out)[:_SHELL_CAP]
    while len(text.encode()) > _SHELL_CAP:
        text = text[:-1]
    return text


@pytest.mark.parametrize("pieces", sorted(_RANDOM_SHELL_PIECES))
@pytest.mark.parametrize("seed", range(4))
def test_random_text_at_the_size_cap_is_checked_in_work_linear_in_its_size(shell_reads, pieces, seed):
    command = _random_shell_text(_RANDOM_SHELL_PIECES[pieces], seed)
    shell_block_reason(command)
    assert shell_reads.chars + shell_reads.words <= _SHELL_WORK_PER_CHAR * len(command)


def _nested_substitutions(depth, filler):
    per = max(1, (_SHELL_CAP - 3 * depth) // (depth * len(filler)))
    return (("$(" + filler * per) * depth + ")" * depth)[:_SHELL_CAP]


@pytest.mark.parametrize("filler", ["a|b ';", "a\\b|c ';", "a|b \";", "a b;"])
@pytest.mark.parametrize("depth", [5, 9, 58, 100])
def test_substitutions_nested_past_the_read_depth_are_refused_before_they_are_read(shell_reads, depth, filler):
    # Each level holds the ones inside it, so reading every level costs the
    # command's size times the depth: past the bound nothing is read.
    command = _nested_substitutions(depth, filler)
    assert shell_block_reason(command) == "could not read the command safely"
    assert shell_reads.chars + shell_reads.words <= 4 * len(command)


@pytest.mark.parametrize("filler", ["a|b ';", "a\\b|c ';", "a|b \";", "a b;"])
@pytest.mark.parametrize("depth", [1, 2, 4])
def test_substitutions_nested_up_to_the_read_depth_are_read_in_bounded_work(shell_reads, depth, filler):
    command = _nested_substitutions(depth, filler)
    assert shell_block_reason(command) is None
    assert shell_reads.chars + shell_reads.words <= _SHELL_WORK_PER_CHAR * len(command)
    # Read, not skipped: the same nesting around a disk write is refused.
    assert shell_block_reason("$(" * depth + "dd if=x of=/dev/sda" + ")" * depth) == _DESTRUCTIVE


def test_a_word_is_looked_up_once_however_many_readings_hold_it(monkeypatch):
    # A nested command's words come back in every reading of every level:
    # what a word names is worked out once per check, and not at all in a
    # reading that holds no program a rule starts from.
    lookups, shells = [], []
    real, real_shells = tool_blocklist._protected_target, tool_blocklist._nested_bodies
    monkeypatch.setattr(tool_blocklist, "_protected_target", lambda t: lookups.append(t) or real(t))
    monkeypatch.setattr(tool_blocklist, "_nested_bodies", lambda words: shells.append(words) or real_shells(words))
    command = _nested_substitutions(4, "find `a` \\; 'b' ~/c ")
    assert shell_block_reason(command) is None
    assert 0 < len(lookups) <= 64 and len(lookups) == len(set(lookups))
    # The nested shells of a reading are looked for once as well.
    assert 0 < len(shells) == len(set(shells))
    del lookups[:]
    assert shell_block_reason(_nested_substitutions(4, "echo `a` \\; 'b' ~/c ")) is None
    assert lookups == []
    # Looked up, not skipped: the same words beside a protected root are refused.
    assert shell_block_reason("$(" * 4 + "find `a` \\; 'b' ~/c ~ -delete " * 9 + ")" * 4) == _DESTRUCTIVE


def test_a_deeply_nested_substitution_never_hides_a_command():
    for depth in (4, 5, 40):
        assert shell_block_reason("echo " + "$(echo " * depth + "rm -rf /" + ")" * depth)
    assert shell_block_reason("echo " + "$(echo " * 4 + "ls" + ")" * 4) is None
    assert shell_block_reason("echo " + "$(echo " * 5 + "ls" + ")" * 5) == "could not read the command safely"
    # Substitutions side by side are no deeper, and neither is one beside a
    # "$(" that never closes (a quoted one).
    assert shell_block_reason("echo " + "$(echo $(echo a)) " * 40) is None
    assert shell_block_reason("grep -c " + "'$(' " * 9 + "f; echo $(echo $(ls))") is None
    # Parentheses that are no substitution add no depth.
    assert shell_block_reason("echo $((" + "(" * 40 + "1" + ")" * 40 + "))") is None


@pytest.mark.parametrize("sql", [
    "DROP TABLE users", "drop database prod;", "select 1; DROP SCHEMA s",
    "TRUNCATE TABLE orders", "truncate orders", "DELETE FROM orders",
    "/* cleanup */ delete from orders;", "-- x\nDROP TABLE t",
    "SELECT 1 DROP TABLE x", "select 1\nGO\ndrop table x", "insert into a values (1); truncate b",
    "TRUNCATE TABLE orders CASCADE", "truncate table a, b", "TRUNCATE ONLY t RESTART IDENTITY",
    "DELETE orders", "delete from only orders",
    "DELETE FROM orders RETURNING *", 'DELETE FROM "Order Items"', 'TRUNCATE TABLE "Order Items"',
    "EXEC('DROP TABLE x')", "delete from [dbo].[Order Items]",
    "DELETE FROM orders o", "DELETE FROM orders AS o", "EXECUTE sp_executesql N'TRUNCATE TABLE t'",
])
def test_destructive_sql_is_blocked(sql):
    assert sql_block_reason(sql)


@pytest.mark.parametrize("text", [
    "SELECT * FROM orders", "DELETE FROM orders WHERE id = 3",
    "Explain TRUNCATE vs DELETE", "update t set a = 1 where id = 2", "",
])
def test_ordinary_text_and_sql_are_not_blocked(text):
    assert sql_block_reason(text) is None


def test_prose_about_drop_table_is_refused_in_sql_but_not_in_search_text():
    # Accepted false refusal (D1): a SQL argument is read as SQL, so DROP TABLE
    # anywhere in it is refused. The same text in a search or prose key passes.
    assert sql_block_reason("how do I drop table x?") == DROP
    assert sql_block_reason("how do I drop table x?", sql_context="shared") is None
    assert sql_block_reason("how do I drop table x?", sql_context="other") is None


@pytest.mark.parametrize("key,text", [
    ("subject", "Delete from inbox"), ("title", "x; truncate the axis"), ("body", "truncate orders"),
    # Search tools share keys like query/q with SQL tools: two-word phrases pass.
    ("query", "delete account"), ("query", "delete user"), ("q", "truncate text"),
    ("command", "delete branch"),
    # Prose that mentions a DROP under a non-SQL key.
    ("title", "Drop table orders_tmp after migration"), ("text", "Drop schema staging once the backfill is done"),
])
def test_prose_outside_sql_arguments_is_not_blocked(key, text):
    assert block_reason(tool_name="send_message", arguments={key: text}) is None


@pytest.mark.parametrize("key,text", [
    ("sql", "DELETE orders"), ("sqlQuery", "DELETE FROM orders"), ("sql_statement", "truncate orders"),
    ("query", "DELETE FROM orders"), ("query", "TRUNCATE TABLE orders"), ("q", "DROP TABLE orders"),
])
def test_sql_in_sql_and_shared_arguments_is_blocked(key, text):
    assert block_reason(tool_name="run", arguments={key: text})


@pytest.mark.parametrize("tool", ["execute_sql", "run_query", "query", "postgres__query", "db_execute"])
@pytest.mark.parametrize("text", ["TRUNCATE orders", "truncate public.orders restart identity", "DELETE orders"])
def test_shared_key_on_a_sql_tool_gets_the_full_sql_rules(tool, text):
    assert block_reason(tool_name=tool, arguments={"query": text})


@pytest.mark.parametrize("tool", ["search_docs", "query_knowledge_base", "search"])
def test_shared_key_on_a_search_tool_stays_lenient(tool):
    assert block_reason(tool_name=tool, arguments={"query": "delete account"}) is None


def test_reading_history_that_mentions_drop_is_not_blocked():
    q = ("SELECT query_text FROM snowflake.account_usage.query_history "
         "WHERE query_text ILIKE 'drop table orders%'")
    assert block_reason(tool_name="execute_sql", arguments={"query": q}) is None
    assert block_reason(tool_name="execute_sql", arguments={"query": "SELECT 'x; DROP TABLE y' AS s"}) is None


def test_block_reason_reads_nested_arguments_and_tool_names():
    assert block_reason(tool_name="run_query", arguments={"opts": {"sql": ["select 1", "DROP TABLE t"]}})
    assert block_reason(tool_name="delete_all_rows", arguments={})
    assert block_reason(tool_name="drop_table", arguments={})
    assert block_reason(tool_name="get_table", arguments={"q": "x"}) is None
    for name in ("purge_all_data", "drop_table_if_exists", "db.drop_table", "srv__wipe_database"):
        assert block_reason(tool_name=name, arguments={}), name
    assert block_reason(tool_name="run", arguments={"sql": "DELETE FROM orders"})
    # Shell patterns only apply to the remote shell tool.
    assert block_reason(tool_name="search", arguments={"command": "rm -rf /"}) is None
    assert block_reason(tool_name="execute_shell", arguments={"command": "rm -rf /"}, remote_shell=True)


# ── blocklist: SQL read with the parser and the token scan ─────────────────

@pytest.mark.parametrize("sql", [
    # CTE deletes.
    "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "WITH x AS (SELECT 1) DELETE FROM t",
    # Quoting and modifiers.
    "DELETE FROM [dbo].[t] AS x", 'DELETE FROM "s"."t"', "DELETE FROM `db`.`t`", "DELETE LOW_PRIORITY FROM t",
    "DELETE @tv", "DELETE FROM t OUTPUT deleted.*",
    # EXEC bodies; the last one fails to parse, so only the token check sees it.
    "EXEC ('DROP TABLE x')", "EXECUTE('TRUNCATE TABLE t')", "EXEC sp_executesql N'DELETE FROM t'",
    "EXEC('EXEC(''DROP TABLE x'')')", "SELECT 1 EXEC('DROP TABLE x')",
    # Batches.
    "SELECT 1 DELETE FROM t", "select 1\ngo\ndelete from t", "SELECT 1\nGO\nTRUNCATE TABLE t",
    "UPDATE t SET a = 1 WHERE id = 2 TRUNCATE TABLE x",
    # Statement starters a regex start-list missed.
    "SET NOCOUNT ON DROP TABLE x", "IF 1=1 DROP TABLE x", "(SELECT 1) DROP TABLE x",
    # MySQL escaped quote.
    "SELECT 'it\\'s'; DROP TABLE x",
    # Procedural and trigger bodies start statements after THEN, AS, BEGIN, DO.
    "DO $$ BEGIN IF true THEN DELETE FROM t; END IF; END $$", "BEGIN IF (TRUE) THEN DELETE FROM t; END IF; END",
    "CREATE TRIGGER tr ON orders AFTER INSERT AS DELETE FROM orders", "CREATE PROCEDURE p AS DELETE FROM t\nGO\nEXEC p",
    "DO $$ BEGIN TRUNCATE t; END $$", "DO $body$ BEGIN TRUNCATE t; END $body$",
    "CREATE PROCEDURE p AS BEGIN TRUNCATE TABLE t END", "IF 1=1 BEGIN TRUNCATE t END",
    "CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO DELETE FROM t", "WHILE 1=1 BEGIN DELETE TOP (10) FROM t END",
    # Parentheses do not hide a missing WHERE.
    "DELETE FROM t USING (SELECT id FROM u WHERE x = 1) s",
    # Statements after SET … ON (T-SQL needs no separator there).
    "SET NOCOUNT ON\nDELETE FROM t", "SET XACT_ABORT ON DELETE FROM t", "SET IDENTITY_INSERT t ON DELETE FROM t",
    # BEFORE and AFTER can be T-SQL table names.
    "SELECT * FROM after DELETE FROM t", "SELECT * FROM before DELETE FROM t",
    # The trigger event is exempt, the body is not.
    "CREATE TRIGGER tr ON t FOR DELETE AS DELETE FROM u",
    # DROP with modifiers.
    "DROP TEMPORARY TABLE t", "DROP DYNAMIC TABLE t", "DROP ICEBERG TABLE t", "DROP EXTERNAL TABLE t",
    "DROP TRANSIENT TABLE t", "DROP FOREIGN TABLE t",
    # MySQL executable comments run their body.
    "/*!50001 DROP TABLE x */", "SELECT 1 /*! TRUNCATE TABLE t */", "SELECT 1; /*!40101 DELETE FROM t */",
    # Dollar-quoted bodies without spaces.
    "DO $$BEGIN TRUNCATE t; END$$", "DO $tag$BEGIN TRUNCATE t; END$tag$",
    "CREATE FUNCTION f() RETURNS void AS $$DELETE FROM t$$ LANGUAGE sql",
    # Beside the `-` and NOT follower exemptions: a comment is never a `-` token.
    "DELETE--x\nFROM t", "DELETE -- note\nFROM t", "SELECT 1 DELETE--x\nFROM t",
    "CREATE TRIGGER tr ON t AFTER DELETE NOT FOR REPLICATION AS DELETE FROM u",
])
def test_destructive_sql_spellings_are_refused(sql):
    assert sql_block_reason(sql), sql


@pytest.mark.parametrize("sql", [
    # DDL that only looks destructive.
    "ALTER TABLE t DROP COLUMN c", "ALTER TABLE t DROP CONSTRAINT fk",
    "CREATE TABLE c (id int REFERENCES p(id) ON DELETE CASCADE)",
    "CREATE TABLE c (id int REFERENCES p(id) ON DELETE SET NULL)",
    # Comments and literals.
    "SELECT * FROM logs -- drop table x", "SELECT 1 /* DROP TABLE x */", "SELECT 'it''s drop table'",
    "SELECT 'it\\'s a drop table'", "INSERT INTO audit VALUES ('delete from orders')",
    # Privileges, MERGE and triggers.
    "GRANT SELECT, DELETE ON t TO r", "REVOKE DELETE ON t FROM r",
    "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE",
    "CREATE TRIGGER tr AFTER DELETE ON t FOR EACH ROW EXECUTE FUNCTION f()",
    # Scoped deletes and functions.
    "DELETE FROM t WHERE id IN (SELECT id FROM u)", "DELETE t FROM t JOIN u ON t.a = u.a WHERE u.x = 1",
    "SELECT TRUNCATE(price, 2) FROM t", "SELECT CASE WHEN a THEN TRUNCATE(price, 2) ELSE 0 END FROM t",
    # Parenthesized subqueries before WHERE.
    "DELETE FROM t USING (SELECT id FROM u) s WHERE t.id = s.id",
    "DELETE t FROM t JOIN (SELECT a FROM u) x ON t.a = x.a WHERE x.a > 1",
    # MERGE clauses after THEN DELETE.
    "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED AND s.x = 1 THEN DELETE "
    "WHEN NOT MATCHED THEN INSERT (id) VALUES (s.id)",
    "MERGE t USING s ON t.id = s.id WHEN MATCHED THEN DELETE OUTPUT deleted.*;",
    "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE RETURNING t.*",
    # Identifiers.
    "SELECT a AS truncate, b FROM t", "SELECT x.delete FROM t x", 'SELECT "delete", [truncate] FROM t',
    # No gate word, even when malformed.
    "SELECT 'unterminated",
    # Literals are never keywords.
    "SELECT * FROM t WHERE op = 'DELETE' AND ts > x", "SELECT CASE WHEN a THEN 'truncate' ELSE 'keep' END FROM t",
    "SELECT N'DELETE FROM t' AS s",
    # FK actions, rules and trigger events.
    "CREATE TABLE c (id int REFERENCES p(id) ON DELETE NO ACTION)",
    "CREATE TABLE c (id int REFERENCES p(id) ON DELETE SET DEFAULT)",
    "CREATE TABLE c (id int REFERENCES p(id) ON DELETE RESTRICT)",
    "CREATE RULE r AS ON DELETE TO t DO INSTEAD NOTHING", "CREATE TRIGGER tr ON t FOR DELETE AS SELECT 1",
    "CREATE TRIGGER tr ON t AFTER DELETE AS SELECT 1",
    "CREATE TRIGGER tr AFTER UPDATE OF c OR DELETE ON t FOR EACH ROW EXECUTE FUNCTION f()",
    "SET NOCOUNT ON;\nDELETE FROM t WHERE id = 1",
    # T-SQL table hints.
    "DELETE FROM t WITH (ROWLOCK) WHERE id = 1",
    "DELETE TOP (1000) FROM t WITH (TABLOCK) WHERE created < '2020-01-01'",
    # The DROP look-ahead stops at ON, punctuation and other kinds.
    "ALTER TABLE t DROP COLUMN c, DROP COLUMN d", "DROP INDEX ix ON t", "DROP MATERIALIZED VIEW v",
    "GRANT DROP ON TABLE t TO u",
    # A MySQL executable comment without a destructive statement.
    "SELECT 1 /*!40101 DELETE FROM t WHERE id = 1 */",
    # File paths and trigger options after DELETE (`-` and NOT followers).
    "source ./delete-old-rows.sql", "\\i delete-old-rows.sql",
    "CREATE TRIGGER tr ON t AFTER DELETE NOT FOR REPLICATION AS SELECT 1",
    "CREATE TRIGGER tr ON t FOR DELETE NOT FOR REPLICATION AS SELECT 1",
    # Not a NOT test: the `,` before DELETE already exempts it.
    "CREATE TRIGGER tr ON t AFTER INSERT, DELETE NOT FOR REPLICATION AS SELECT 1",
])
def test_ordinary_sql_spellings_are_not_refused(sql):
    assert sql_block_reason(sql) is None, sql


@pytest.mark.parametrize("text", [
    "delete account", "delete user", "truncate text", "delete branch", "DELETE FROM orders WHERE id = 1",
    "delete from my history the old items", "truncate table names longer than 20 chars",
    "drop the table from the deck",
])
def test_search_phrases_pass_on_a_search_tool(text):
    assert block_reason(tool_name="search_docs", arguments={"query": text}) is None


@pytest.mark.parametrize("text", [
    "SELECT 1; DROP TABLE x",
    "Delete from inbox",  # known limit, accepted
    "x; DROP SCHEMA s",
    "DELETE FROM t USING u",  # only the parse tree sees it
])
def test_sql_in_a_search_tool_query_is_refused(text):
    assert block_reason(tool_name="search_docs", arguments={"query": text})


# Parity: shared-key texts that no SQL tokenizer can read keep today's result.
# The recorded values are the results of the module at 3875d2971 (probe r5 §1).
# Do not edit them to make this test pass: a change here is a behaviour change.
_PARITY_SHARED = [
    ("DROP TABLE x; /*", DROP), ("DROP TABLE x; /*/", DROP), ("DROP TABLE x; SELECT 'a", DROP),
    ("DROP TABLE x; /* a */ /*", DROP), ("x; SELECT '/*'; DROP TABLE x; SELECT 'a", DROP),
    ("x /* /* */ ; DROP TABLE y; SELECT 'a", DROP), ("x; EXEC('DROP TABLE y'); SELECT 'a", DROP),
    ("x\ngo\nDROP TABLE y; /*", DROP), ("DROP TABLE x -- note\n; /*", DROP),
    ("DELETE FROM orders; /*", DELETE), ('DELETE FROM "Order Items"; /*', DELETE),
    ("DELETE FROM `my-project.ds.t`; /*", DELETE), ("DELETE FROM orders o; /*", DELETE),
    ("x; EXEC('DELETE orders'); SELECT 'a", DELETE), ("Delete from inbox; it's urgent", DELETE),
    ('TRUNCATE TABLE "Order Items"; SELECT \'a', TRUNCATE), ("x; EXEC('TRUNCATE orders'); SELECT 'a", TRUNCATE),
    ("x; /* DROP TABLE y", None), ("why did revenue drop? what's the cause", None),
    ("why did revenue drop? /* what's next", None), ("delete from my history the old item's", None),
    ("truncate table names that aren't used", None), ("don't drop the table", None),
    ("DELETE FROM orders WHERE id = 1; /*", None),
]


@pytest.mark.parametrize("text,today", _PARITY_SHARED)
def test_unreadable_shared_text_keeps_todays_result(text, today):
    assert _every_tokenizer_raises(text)
    assert sql_block_reason(text, sql_context="shared") == today
    for tool in ("search_docs", "query_knowledge_base"):
        assert block_reason(tool_name=tool, arguments={"query": text}) == today, tool
    # Deliberate change (Nir: "Tool schema + names"): on a tool named for
    # running SQL (write_query, read_query) this is SQL, and unreadable SQL with
    # a gate word is refused. At 3875d2971 these two gave the shared result.
    for tool in ("write_query", "read_query"):
        assert block_reason(tool_name=tool, arguments={"query": text}) == UNREADABLE, tool


def test_unreadable_shared_text_without_its_gate_words_passes():
    # EXEC with no drop/truncate/delete fails the shared gate, so it never reaches
    # the parity fallback; today passes it too.
    text = "x; EXEC('SELECT 1'); SELECT 'a"
    assert _every_tokenizer_raises(text)
    assert sql_block_reason(text, sql_context="shared") is None


@pytest.mark.parametrize("text,today", [
    ("DROP TABLE [it's], `a\"b`", DROP), ("DROP TABLE x; /*", None), ("DROP TABLE x; SELECT 'a", None),
])
def test_unreadable_other_text_keeps_todays_result(text, today):
    # Recorded from the module at 3875d2971 (probe r5 §3 and probe c).
    assert _every_tokenizer_raises(text)
    assert sql_block_reason(text, sql_context="other") == today
    assert block_reason(tool_name="save_note", arguments={"title": text}) == today


def test_a_single_drop_over_the_parse_limit_keeps_todays_result_at_other():
    # Readable, but over 200 KB, so the parser is skipped and today's rule decides.
    text = "DROP TABLE " + ", ".join(f"t{i}" for i in range(30_000))
    assert len(text.encode()) == 228_899
    assert sql_block_reason(text, sql_context="other") == DROP
    assert block_reason(tool_name="save_note", arguments={"title": text}) == DROP


def test_an_unreadable_other_text_over_the_scan_cap_still_gets_todays_rule():
    # Level `other` has no scan cap: the parity fallback runs without tokenizing.
    big = "DROP TABLE [it's], " + ", ".join(f"t{i}" for i in range(150_000))
    assert len(big.encode()) > 1024 * 1024
    assert sql_block_reason(big, sql_context="other") == DROP
    assert sql_block_reason(big + "; /*", sql_context="other") is None


def test_many_dead_comment_openers_are_checked_in_linear_time():
    text = "DROP TABLE x; " + "/* " * 300_000
    assert len(text) == 900_014
    assert block_reason(tool_name="search_docs", arguments={"query": text}) == DROP


def test_promotion_follows_todays_sql_start_rule():
    # re.IGNORECASE reads the dotted capital I as "i", so today's rule promotes
    # this text (and refused it); every tokenizer raises, so it is unreadable SQL.
    assert block_reason(tool_name="search_docs", arguments={"query": "İNSERT x; TRUNCATE orders; SELECT 'a"})


@pytest.mark.parametrize("tool", ["write_query", "read_query", "run_snowflake_query"])
@pytest.mark.parametrize("text", [
    "INSERT INTO a VALUES (1); TRUNCATE b", "EXEC('TRUNCATE orders')", "SELECT 1; DELETE orders",
    "SELECT 1 DROP TABLE x", "select 1\nGO\ndrop table x",
])
def test_shared_key_sql_on_a_sql_tool_the_name_rule_misses_is_refused(tool, text):
    assert block_reason(tool_name=tool, arguments={"query": text})


def test_other_context_refuses_only_a_lone_drop():
    assert sql_block_reason("Drop table orders_tmp after migration", sql_context="other") is None
    assert sql_block_reason("drop table x; select 1", sql_context="other") is None
    assert sql_block_reason("DROP TABLE x", sql_context="other") == DROP


def test_size_limits():
    big_select = "SELECT 1;" * 23_000
    assert len(big_select) > 200 * 1024
    assert sql_block_reason(big_select + " DROP TABLE x") == DROP  # parser skipped, tokens decide
    assert sql_block_reason("SELECT a FROM t;" * 14_000) is None
    # Accepted false refusal (D8): over the 1 MB scan cap with a gate word.
    assert sql_block_reason("SELECT 1; " * 110_000 + "delete") == UNREADABLE


def test_execute_bodies_are_read_when_the_parser_is_skipped():
    # Every tokenizer reads EXECUTE as a command and its rest as one raw string;
    # over 200 KB only the token check runs, and it must still read the body.
    big_select = "SELECT 1;" * 23_000
    assert sql_block_reason(big_select + " EXECUTE('TRUNCATE TABLE t')") == TRUNCATE
    assert sql_block_reason(big_select + " EXECUTE sp_executesql N'DELETE FROM t'") == DELETE
    assert sql_block_reason(big_select + " EXECUTE('SELECT 1')") is None


def test_unreadable_sql_with_a_gate_word_is_refused():
    assert sql_block_reason("DELETE FROM t WHERE a = 'unterminated") == UNREADABLE


def test_a_parser_crash_leaves_the_token_check_to_decide():
    # Deep nesting makes the parser raise RecursionError, not a sqlglot error.
    assert sql_block_reason("(" * 5000 + "SELECT 1" + ")" * 5000 + "; DROP TABLE x") == DROP
    assert sql_block_reason("SELECT 1 DROP TABLE x") == DROP


@pytest.mark.parametrize("command,today", [
    # Parity: the unreadable shell token gets today's result (probe r5 §2).
    ("echo \"DROP TABLE x; SELECT 'a\" | psql", DROP), ('echo "DROP TABLE x; /*" | psql', DROP),
    ('echo "DROP TABLE x; /*/" | psql', DROP), ('echo "DELETE FROM orders; /*" | psql', DELETE),
    ("echo 'DELETE FROM \"Order Items\"; /*' | psql", DELETE),
    ("echo 'DELETE FROM `my-project.ds.t`; /*' | bq query --nouse_legacy_sql", DELETE),
    ("echo \"TRUNCATE TABLE orders; SELECT 'a\" | psql", TRUNCATE),
    ("echo 'TRUNCATE TABLE \"Order Items\"; SELECT '\"'\"'a' | psql", TRUNCATE),
    ('echo "x; /* DROP TABLE y" | psql', None),
])
def test_unreadable_sql_piped_into_a_db_client_keeps_todays_result(command, today):
    assert shell_block_reason(command) == today


@pytest.mark.parametrize("command", [
    'psql -c "WITH d AS (DELETE FROM t RETURNING 1) SELECT 1"',
    'echo "SELECT 1; TRUNCATE orders" | psql',  # promoted, as today
])
def test_more_sql_through_a_db_client_is_refused(command):
    assert shell_block_reason(command)


@pytest.mark.parametrize("command", [
    'mysql -e "SELECT 1; GRANT SELECT, DELETE ON t TO r"', "psql -d db -f delete.sql",
    'psql -c "select 1" > truncate.log', 'echo "it\'s done" | psql -c "select 1"',
    'psql -c "select 1" && echo "delete step done"', "psql -d db -f ./delete-old-rows.sh",
    './delete-old-rows.sh && psql -c "select 1"', 'mysql -e "source ./delete-old-rows.sql"',
    'psql -c "\\i delete-old-rows.sql"',
    # Known limits, as today: a heredoc body and unquoted words are separate
    # shell tokens, so no single token holds the statement. Named in the PR.
    "psql <<'SQL'\nDROP TABLE x;\nSQL", "echo DROP TABLE x | psql",
])
def test_ordinary_db_client_commands_pass(command):
    assert shell_block_reason(command) is None


# Accepted false refusals: each is refused on purpose, so a later change shows
# up as a deliberate one. "Today" is the module at 3875d2971 (probe r5 §5).
@pytest.mark.parametrize("sql", [
    "how do I drop table x?",  # D1, new
    "SELECT [drop table] FROM t",  # catalog: `[` is punctuation outside T-SQL, new
    "CREATE TRIGGER tr ON t AFTER DELETE WITH APPEND AS SELECT 1",  # catalog: WITH is no follower, new
    "SELECT [delete] FROM t",  # catalog: only tsql reads [delete] as a name, new
    "drop the temp table",  # catalog: modifier look-ahead, new
    "/*! drop table */",  # catalog: the /*! copy is read in every dialect, new
    "SELECT a AS delete FROM t",  # catalog: AS DELETE FROM is also a procedure body, new
    "SELECT 1 # drop table x",  # catalog: # is a comment only in MySQL, as today
    "EXEC sp_log 'drop table happened'",  # catalog: EXEC literals are rechecked, as today
    "sp_log 'drop table happened'",  # a procedure call without EXEC: its literals are rechecked, new
])
def test_accepted_false_refusals_in_sql(sql):
    assert sql_block_reason(sql)


@pytest.mark.parametrize("text", [
    "update on the delete account flow",  # D10, new
    "with delete pending, then archive",  # D10, new
    "Delete from inbox",  # catalog, as today
    # A temporary procedure's name (#run) is read on shared keys too; a
    # hashtag-led phrase with quoted SQL is refused.
    "#run 'DROP TABLE x' failed",
])
def test_accepted_false_refusals_on_a_search_tool(text):
    assert block_reason(tool_name="search_docs", arguments={"query": text})


@pytest.mark.parametrize("text", [
    # A word, then a quoted text: a procedure call without EXEC in SQL, but on
    # a shared key the procedure-call rule reads only names a search phrase
    # does not start with, so a search phrase passes.
    "errors 'drop table' today", "user said 'TRUNCATE TABLE orders'", "logs containing 'DELETE FROM' last week",
    "find 'DELETE FROM orders' in the logs", "customers who typed 'delete from orders'",
    "call log 'DELETE FROM orders'", "\"refund\" 'DELETE FROM orders'",
])
@pytest.mark.parametrize("tool", ["search_docs", "query_knowledge_base"])
def test_quoted_sql_in_a_search_phrase_passes(tool, text):
    assert block_reason(tool_name=tool, arguments={"query": text}) is None, (tool, text)


@pytest.mark.parametrize("tool,key", [("mssql_execute", "input"), ("run_sql", "text"), ("execute", "query"),
                                      ("search_docs", "query")])
@pytest.mark.parametrize("call", [
    # Names a search phrase does not start with: qualified, bracketed, a
    # temporary procedure's, or sp_/xp_.
    "sp_MSforeachtable 'DROP TABLE ?'", "dbo.run N'DELETE FROM x'", "[dbo].[run] N'DELETE FROM x'",
    "[run] N'TRUNCATE TABLE x'", "#run N'DROP TABLE x'", "##run N'DROP TABLE x'", "xp_runner 'TRUNCATE TABLE x'",
    "CALL dbo.run('DELETE FROM t')", "CALL sp_run('DROP TABLE t')", "CALL [run]('DROP TABLE t')",
    "CALL #run('DROP TABLE t')",
])
def test_a_procedure_call_on_a_shared_key_is_read_when_its_name_is_no_word(tool, key, call):
    assert block_reason(tool_name=tool, arguments={key: call}), (tool, key, call)


def test_accepted_false_refusal_of_other_text_on_a_sql_tool():
    # New: every non-query argument of a SQL tool is checked at the shared level.
    assert block_reason(tool_name="run_query", arguments={"title": "Delete from staging", "sql": "select 1"}) == DELETE


def test_promoted_unreadable_search_text_is_refused_as_unreadable():
    # Accepted false refusal (D10, new): promoted to SQL, and no tokenizer reads it.
    assert block_reason(tool_name="search_docs", arguments={"query": "Select John's tickets marked delete"}) == UNREADABLE


@pytest.mark.parametrize("command", [
    'psql -f x.sql && echo "Begin delete of old rows"',  # D10, new
    'psql -c "select 1" && echo "Update done - delete step next"',  # D10, new
])
def test_accepted_false_refusals_beside_a_db_client(command):
    assert shell_block_reason(command)


def _assert_reads_text_a_bounded_number_of_times(reads, text):
    size = len(text.encode())
    assert reads.tokenize_bytes <= _READS_PER_DIALECT * len(FIVE_DIALECTS) * size, (reads, size)
    assert reads.parse_bytes <= _READS_PER_DIALECT * len(FIVE_DIALECTS) * size, (reads, size)


@pytest.mark.parametrize("k", [7000, 28000])
def test_nested_deletes_are_read_a_bounded_number_of_times(sql_reads, k):
    text = "DELETE FROM t USING (" * k + "SELECT 1" + ") s WHERE 1" * k
    assert sql_block_reason(text, sql_context="sql") is None
    _assert_reads_text_a_bounded_number_of_times(sql_reads, text)


# ── blocklist: EXEC rechecks, analysis caps and dynamic SQL ────────────────

def _many_literals(k):
    return "EXEC p " + ",".join("'drop%d'" % i for i in range(k))


@pytest.mark.parametrize("text,expect", [
    # 20,000 literals to recheck, past the 64 rechecks of one call: refused
    # before any of them is read.
    pytest.param(_many_literals(20_000), UNREADABLE, id="many-literals"),  # 228,896 bytes
    pytest.param(_many_literals(4_000), UNREADABLE, id="4000-literals"),
    pytest.param("".join("EXEC p%d; " % i for i in range(2_000)), None, id="2000-exec-statements"),
    pytest.param("".join("EXEC p%d; " % i for i in range(8_000)), None, id="8000-exec-statements"),
])
def test_many_exec_literals_cost_about_one_read_of_the_text(sql_reads, text, expect):
    # Each literal is rechecked alone, at most 64 per call, so the check reads
    # about the text itself whatever it holds: no literal read past the
    # bound, and no read of the whole text per literal.
    assert block_reason(tool_name="search_docs", arguments={"query": text}) == expect
    _assert_reads_text_a_bounded_number_of_times(sql_reads, text)
    # The text itself, plus at most 64 rechecks, in each dialect and copy.
    assert sql_reads.tokenize_calls <= _READS_PER_DIALECT * len(FIVE_DIALECTS) * (1 + 64)


def test_literals_to_recheck_past_the_byte_allowance_are_refused_at_once(sql_reads):
    # Five 197 KB literals to run, each holding a gate word (985,023 bytes in
    # all): the rechecks of one call may read at most 1 MB, and these five plus
    # their concatenation are about 2 MB, so the call is refused before any is
    # read. Accepted false refusal (a cost bound, like D8).
    text = "sp_x " + ", ".join(
        "'" + "".join(f"select 1 as deleted{k}_{i}; " for i in range(12000))[:197_000] + "'" for k in range(5))
    assert len(text.encode()) == 985_023
    assert block_reason(tool_name="mssql_query", arguments={"query": text}) == UNREADABLE
    # Only the text itself was read: no literal was.
    assert sql_reads.tokenize_bytes <= _READS_PER_DIALECT * len(FIVE_DIALECTS) * len(text.encode())
    assert sql_reads.tokenize_calls <= _READS_PER_DIALECT * len(FIVE_DIALECTS)
    # Below the allowance the literals are read: one 197 KB literal passes.
    one = "sp_x '" + "".join(f"select 1 as deleted_{i}; " for i in range(12000))[:197_000] + "'"
    assert block_reason(tool_name="mssql_query", arguments={"query": one}) is None


def _exec_statements(k, start=0):
    return "; ".join("EXEC('drop%d')" % i for i in range(start, start + k))


def test_more_rechecks_than_the_call_bound_are_refused():
    # Accepted false refusal (a cost bound): each literal to run is rechecked
    # alone, at most 64 rechecks per call across every level and argument.
    assert sql_block_reason(_exec_statements(64)) is None
    assert sql_block_reason(_exec_statements(65)) == UNREADABLE
    # The bound is per call, not per argument.
    two = {"sql": _exec_statements(40), "statement": _exec_statements(40, start=40)}
    assert block_reason(tool_name="run", arguments={"sql": two["sql"]}) is None
    assert block_reason(tool_name="run", arguments=two) == UNREADABLE


_VERBS = [("DELETE FROM orders", DELETE), ("DROP TABLE orders", DROP), ("TRUNCATE TABLE orders", TRUNCATE)]


@pytest.mark.parametrize("verb,reason", _VERBS)
@pytest.mark.parametrize("shape", [
    "DECLARE @a NVARCHAR(99) = 'deleted {#'; DECLARE @b NVARCHAR(99) = '%s --#}'; EXEC(@b)",
    "EXEC('deleted {#'); EXEC('%s --#}')",
    "EXEC sp_executesql N'deleted {#'; EXEC sp_executesql N'%s --#}'",
])
def test_a_block_comment_opened_in_one_literal_never_hides_the_next(shape, verb, reason):
    sql = shape % verb
    assert sql_block_reason(sql) == reason, sql
    assert block_reason(tool_name="mssql_query", arguments={"query": sql}) == reason, sql


@pytest.mark.parametrize("opener,closer", [
    ("{#", "#}"), ("--", ""), ("#", ""), ("//", ""), ("/*", "*/"), ("\"", "\""), ("[", "]"), ("`", "`"),
    ("$$", "$$"), ("$t$", "$t$"), ("{{", "}}"), ("{%", "%}"),
])
@pytest.mark.parametrize("verb,reason", _VERBS)
def test_an_unclosed_construct_in_one_literal_never_reaches_a_later_one(opener, closer, verb, reason):
    # Each literal is read on its own: nothing in one can open a quote, name or
    # comment that runs into the next. (A lone closing quote can make the
    # later literal itself unreadable, which is refused too.)
    for sql in ("EXEC('deleted %s'); EXEC('%s %s')" % (opener, verb, closer),
                "EXEC p 'deleted %s', '%s %s'" % (opener, verb, closer)):
        assert sql_block_reason(sql) in (reason, UNREADABLE), sql
    assert sql_block_reason("EXEC('deleted %s'); EXEC('%s')" % (opener, verb)) == reason


@pytest.mark.parametrize("sql", [
    # Each literal is rechecked on its own, and split strings are still joined.
    "EXEC p 'x', 'DROP TABLE orders'", "EXEC('DR' + 'OP TABLE orders')",
    "EXEC sp_executesql N'DELETE FROM t WHERE n = ''a''', N'TRUNCATE TABLE u'",
    "EXEC p 'it''s'; EXEC('DROP TABLE orders')",
    # A literal that opens a quote, name or comment never hides the next one.
    "EXEC p 'drop \"'; EXEC('DROP TABLE orders'); EXEC q '\" drop'",
    "EXEC p 'drop /*'; EXEC('DROP TABLE orders'); EXEC q 'drop */'",
    "EXEC p 'drop ['; EXEC('DROP TABLE orders'); EXEC q 'drop ]'",
    "EXEC p 'drop $$'; EXEC('DROP TABLE orders'); EXEC q 'drop $$'",
    # Dynamic SQL whose text sits in another statement.
    "PREPARE s FROM 'DROP TABLE orders'; EXECUTE s;",
    "DECLARE @q NVARCHAR(99) = 'DROP TABLE orders'; EXEC(@q)",
    "DECLARE @q NVARCHAR(99); SET @q = 'TRUNCATE TABLE orders'; EXEC sp_executesql @q",
    "DECLARE @a NVARCHAR(9) = 'DR'; DECLARE @b NVARCHAR(99) = 'OP TABLE orders'; EXEC(@a + @b)",
    "SET @q = 'DELETE FROM orders'; PREPARE s FROM @q; EXECUTE s",
    "PREPARE s AS DELETE FROM orders",
    # Every tokenizer keeps the rest of an EXECUTE statement as one raw string;
    # a T-SQL batch needs no separator, so the rest is read as SQL too.
    "EXECUTE p DROP TABLE orders", "EXECUTE('SELECT 1') DROP TABLE orders",
])
def test_exec_literals_and_dynamic_sql_are_read(sql):
    assert sql_block_reason(sql), sql


@pytest.mark.parametrize("sql", [
    "EXEC p 'hello'", "EXEC p0; EXEC p1; EXEC p2", "EXEC p 'deleted rows', 'x'",
    "INSERT INTO log VALUES ('delete from orders'); EXEC p",
    "DECLARE @n INT = 1; EXEC p @n", "PREPARE s FROM 'SELECT * FROM orders'; EXECUTE s",
    "DECLARE @q NVARCHAR(99) = 'SELECT 1'; EXEC(@q)",
    "EXEC('EXEC(''EXEC(''''SELECT 1'''')'')')",
    # Procedure names that hold a gate word are read once, not refused as
    # nested past the recheck depth.
    "EXECUTE dbo.delete_old_rows", "EXEC dbo.delete_old_rows; EXEC dbo.drop_stale_rows",
    "EXECUTE dbo.delete_old_rows; EXECUTE dbo.drop_stale_rows; EXECUTE dbo.truncate_log_rows",
])
def test_ordinary_exec_and_prepared_statements_pass(sql):
    assert sql_block_reason(sql) is None, sql


def test_more_quoted_exec_literals_than_the_recheck_bound_are_refused():
    # Accepted false refusal (a cost bound, like D8): each literal is
    # rechecked alone, at most 64 per call; past that the text is refused.
    def text(k):
        return "EXEC p " + ", ".join("'drop \"t%d\"'" % i for i in range(k))

    assert sql_block_reason(text(60)) is None  # 60 literals and their concatenation
    assert sql_block_reason(text(70)) == UNREADABLE


def test_exec_nested_past_the_recheck_depth_is_refused():
    # Three levels deep: the third body is past the recheck depth, so the
    # check cannot read it and refuses instead of passing it.
    assert block_reason(tool_name="run", arguments={"sql": "EXEC('EXEC(''EXEC(''''DROP TABLE orders'''')'')')"})


@pytest.mark.parametrize("sql", [
    # A harmless spelling of a statement (no spaces, other case) read at one
    # level never hides the real statement one level deeper.
    "EXEC('DROPTABLE x'); EXEC('EXEC(''DROP TABLE x'')')",
    "EXEC('EXEC(''DROP TABLE x'')'); EXEC('DROPTABLE X')",
    "EXEC('truncatetable x'); EXEC('EXEC(''TRUNCATE TABLE x'')')",
    "EXEC('DELETEFROM t'); EXEC('EXEC(''DELETE FROM t'')')",
])
@pytest.mark.parametrize("tool,key", [("execute_sql", "sql"), ("run_query", "query")])
def test_a_near_copy_of_a_literal_never_hides_the_one_inside_it(tool, key, sql):
    assert block_reason(tool_name=tool, arguments={key: sql}), sql


_NESTED_PAYLOADS = ["DROP TABLE x", "TRUNCATE TABLE x", "DELETE FROM t", "drop schema s", "Truncate Table t",
                    "DELETE FROM t\n"]


def _near_copies(p):
    return {re.sub(r"\s+", "", p), p.upper(), p.lower(), p.swapcase(), re.sub(r"\s+", "", p).lower(),
            p.replace(" ", "  "), " " + p}


@pytest.mark.parametrize("payload", _NESTED_PAYLOADS)
def test_near_copies_never_hide_a_nested_statement(payload):
    for near in _near_copies(payload) - {payload}:
        inner = "EXEC('EXEC(''%s'')')" % payload
        outer = "EXEC('%s')" % near
        for sql in (inner + "; " + outer, outer + "; " + inner):
            assert sql_block_reason(sql), sql


@pytest.mark.parametrize("sql", [
    # A line comment at the end of one literal never swallows the next one.
    "EXEC('SELECT deleted OR --'); EXEC('DELETE FROM orders')",
    "EXEC('SELECT 1 AS dropped, t. --'); EXEC('DELETE FROM orders')",
    "DECLARE @a nvarchar(50) = 'SELECT deleted OR --'; DECLARE @b nvarchar(50) = 'DELETE FROM orders'; EXEC(@b)",
    "EXEC('SELECT deleted OR --'); EXEC('TRUNCATE TABLE orders')",
    "EXEC('SELECT deleted OR --'); EXEC('DROP TABLE orders')",
    "EXEC('SELECT deleted OR #'); EXEC('DELETE FROM orders')",
    "EXEC('SELECT deleted OR #'); EXEC('TRUNCATE TABLE orders')",
    "EXEC('SELECT dropped OR #'); EXEC('DROP TABLE orders')",
    "EXEC('SELECT deleted OR //'); EXEC('DELETE FROM orders')",
    "EXEC('SELECT deleted OR //'); EXEC('TRUNCATE TABLE orders')",
    "EXEC('SELECT dropped OR //'); EXEC('DROP TABLE orders')",
])
def test_a_line_comment_in_one_literal_never_hides_the_next(sql):
    assert sql_block_reason(sql), sql


@pytest.mark.parametrize("sql", [
    # T-SQL runs sp_executesql without EXEC as the first statement of a batch.
    "sp_executesql N'DROP TABLE x'", "SELECT 1\nGO\nsp_executesql N'DROP TABLE x'",
    "sys.sp_executesql N'TRUNCATE TABLE x'", "master..sp_executesql N'DELETE FROM t'",
    "sp_execute N'DROP TABLE x'",
    "DECLARE @q NVARCHAR(99) = 'DROP TABLE orders'\nGO\nsp_executesql @q",
])
def test_sp_executesql_without_exec_is_read(sql):
    assert sql_block_reason(sql), sql


@pytest.mark.parametrize("call", [
    "sp_MSforeachtable 'DELETE FROM ?'", "sp_MSforeachdb N'DROP TABLE [?].dbo.x'",
    "sp_prepexec NULL, NULL, N'DROP TABLE x'", "sp_prepexec @h OUTPUT, NULL, N'TRUNCATE TABLE x'",
    "sys.sp_MSforeachtable @command1 = N'DELETE FROM ?'",
    # Names on no list: any procedure runs its arguments' SQL the same way.
    "dbo.my_runner 'TRUNCATE TABLE x'", "[dbo].[my_runner] N'DROP TABLE x'", "run_later -1, 'DELETE FROM t'",
])
def test_a_procedure_called_without_exec_has_its_literals_read(call):
    # T-SQL runs any procedure without EXEC as the first statement of a batch.
    for sql in (call, "SELECT 1\nGO\n" + call, "EXEC " + call):
        assert sql_block_reason(sql), sql
    # In every SQL argument: a SQL key, a SQL tool's query, a described parameter.
    schema = {"type": "object", "properties": {"text": {"type": "string", "description": "SQL to run"}}}
    assert block_reason(tool_name="run", arguments={"sql": call}), call
    assert block_reason(tool_name="mssql_query", arguments={"query": call}), call
    assert block_reason(tool_name="lookup", arguments={"text": call}, input_schema=schema), call


@pytest.mark.parametrize("sql,reason", [
    # CALL runs a procedure: its literals are SQL it may run.
    ("CALL run_sql('DROP TABLE x')", DROP), ("CALL dbo.run('DELETE FROM t')", DELETE),
    ("CALL run_sql(N'TRUNCATE TABLE x')", TRUNCATE),
    # T-SQL temporary procedures (#local, ##global), also schema-qualified.
    ("#run N'DROP TABLE x'", DROP), ("##run N'DROP TABLE x'", DROP), ("dbo.#run N'DROP TABLE x'", DROP),
    ("##run 'DELETE FROM t'", DELETE),
])
def test_call_and_temporary_procedures_have_their_literals_read(sql, reason):
    assert sql_block_reason(sql) == reason, sql
    assert block_reason(tool_name="run_sql", arguments={"sql": sql}) == reason, sql


@pytest.mark.parametrize("sql", [
    "CALL dbo.run('SELECT 1')", "CALL refresh_stats()", "##run N'SELECT 1'",
    # A space after # is MySQL's comment, not a temporary procedure's name.
    "# note 'drop table x'",
])
def test_harmless_call_and_temporary_procedures_pass(sql):
    assert sql_block_reason(sql) is None, sql


@pytest.mark.parametrize("sql", ["CALL p('DR' || 'OP TABLE x')", "CALL p(CONCAT('DR','OP TABLE x'))",
                                 "CALL p('TRUNC' || 'ATE TABLE x')"])
def test_a_call_with_split_literals_is_read_joined(sql):
    # CALL is a gate word: the joined literals are read like EXEC's.
    assert sql_block_reason(sql), sql
    assert block_reason(tool_name="run_sql", arguments={"sql": sql}), sql


@pytest.mark.parametrize("sql", ["CALL p('a' || 'b')", "CALL refresh_stats()", "recall the callback"])
def test_a_harmless_call_with_split_literals_passes(sql):
    assert sql_block_reason(sql) is None, sql


@pytest.mark.parametrize("sql", [
    # Statements that start with a keyword are not procedure calls.
    "SELECT 'DROP TABLE x'", "SET @q = 'DROP TABLE orders'", "VALUES ('DELETE FROM orders')",
    "INSERT INTO log VALUES ('truncate table orders')", "COMMENT ON TABLE t IS 'drop table t when done'",
    "DECLARE @q NVARCHAR(99) = 'DELETE FROM orders'", "IF @x = 'DROP TABLE x' SELECT 1",
    "PRINT 'DROP TABLE x'", "THROW 50000, 'DELETE FROM orders failed', 1", "RAISERROR('DROP TABLE x', 16, 1)",
    "RAISE NOTICE 'DELETE FROM orders skipped'", "RAISE EXCEPTION 'DROP TABLE x failed'",
    "UPDATE t SET note = 'DELETE FROM orders' WHERE id = 1",
    # A procedure call whose literals hold no destructive SQL passes.
    "sp_MSforeachtable 'SELECT COUNT(*) FROM ?'", "dbo.my_runner 'deleted rows'",
])
def test_statements_starting_with_a_keyword_are_not_procedure_calls(sql):
    assert sql_block_reason(sql) is None, sql
    assert block_reason(tool_name="run_sql", arguments={"sql": sql}) is None, sql


def test_sp_executesql_with_harmless_sql_passes():
    assert sql_block_reason("sp_executesql N'SELECT 1'") is None
    assert sql_block_reason(
        "SELECT 1\nGO\nsp_executesql N'SELECT * FROM t WHERE deleted = @n', N'@n int', @n = 1") is None


def test_the_quoted_literal_recheck_bound_counts_the_concatenation():
    # 64 rechecks per call, and the concatenation of the literals is one of
    # them: 63 quoted literals pass, 64 are refused.
    def text(k):
        return "EXEC p " + ", ".join("'drop \"t%d\"'" % i for i in range(k))

    assert sql_block_reason(text(63)) is None
    assert sql_block_reason(text(64)) == UNREADABLE


def test_arguments_nested_past_the_walk_depth_are_refused():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"sql": "DROP TABLE orders"}}}}}}}
    assert block_reason(tool_name="run_query", arguments=deep) == DROP
    assert block_reason(tool_name="run_query", arguments={"sql": [[[[[["DROP TABLE orders"]]]]]]}) == DROP
    # Past the walk's depth cap: refused as unreadable, never skipped.
    value: object = "x"
    for _ in range(80):
        value = [value]
    assert block_reason(tool_name="search_docs", arguments={"query": value}) == UNREADABLE
    # A deep but ordinary search body still passes.
    es = {"query": {"bool": {"must": [{"bool": {"should": [{"match": {"title": "delete account"}}]}}]}}}
    assert block_reason(tool_name="search_docs", arguments=es) is None


@pytest.mark.parametrize("tool,arguments,schema", [
    # A string inside an object keeps the stronger of its own key's kind and
    # its enclosing parameter's kind.
    ("submit", {"sql": {"q1": "DELETE FROM orders"}}, None),
    ("submit", {"sql": {"batch": {"steps": ["SELECT 1", "TRUNCATE orders"]}}}, None),
    ("lookup", {"req": {"body": "DELETE orders"}},
     {"type": "object", "properties": {"req": {"type": "object", "description": "SQL to run"}}}),
    ("run_query", {"queries": [{"text": "DELETE orders"}]}, None),
    ("search_docs", {"filter": {"sql": "DELETE orders"}}, None),  # an inner SQL key, as before
])
def test_a_string_nested_in_a_sql_argument_gets_the_sql_rules(tool, arguments, schema):
    assert block_reason(tool_name=tool, arguments=arguments, input_schema=schema), arguments


def test_a_string_nested_in_a_search_argument_stays_lenient():
    es = {"query": {"bool": {"must": [{"match": {"title": "delete account"}}, {"term": {"x": "truncate text"}}]}}}
    assert block_reason(tool_name="search_docs", arguments=es) is None
    assert block_reason(tool_name="send_message", arguments={"meta": {"note": "Delete from inbox"}}) is None


def test_a_lone_surrogate_never_breaks_the_check():
    # A lone surrogate (invalid UTF-8) never raises. At "sql" and "shared" a
    # text with one and a gate word is refused as unreadable (fail closed: an
    # engine could drop the surrogate and run the rest). Without a gate word it
    # passes, at every level.
    assert block_reason(tool_name="run_sql", arguments={"sql": "DROP TABLE x \ud800"}) == UNREADABLE
    assert block_reason(tool_name="search_docs", arguments={"query": "drop table x \ud800"}) == UNREADABLE
    assert block_reason(tool_name="save_note", arguments={"title": "DROP TABLE x -- \ud800"}) == DROP
    assert block_reason(tool_name="save_note", arguments={"title": "DROP TABLE \"x\ud800\""}) == DROP
    # At "other" the text is read as it is: a surrogate before or after the
    # DROP is junk, like any other character there, so it is no lone DROP.
    for junk in ("\ud800", "é"):
        assert block_reason(tool_name="save_note", arguments={"title": "DROP TABLE x " + junk}) is None
        assert block_reason(tool_name="save_note", arguments={"title": junk + "DROP TABLE x"}) is None
    assert sql_block_reason("EXEC('DELETE FROM t \udfff')") == UNREADABLE
    for key in ("sql", "query", "title"):
        assert block_reason(tool_name="run_sql", arguments={key: "select \ud800"}) is None, key


# ids: pytest-xdist cannot send a test id holding a lone surrogate.
@pytest.mark.parametrize("text", ["\ud800DELETE FROM x", "SELECT 1;\ud800DELETE FROM x", "EXEC('\ud800DELETE FROM x')",
                                  "\udfffTRUNCATE TABLE x"], ids=["lead", "after_select", "in_exec", "truncate"])
def test_a_lone_surrogate_with_a_gate_word_is_unreadable_in_sql_and_shared_text(text):
    # ascii(): the failure message is sent between xdist workers as UTF-8.
    assert block_reason(tool_name="run_sql", arguments={"sql": text}) == UNREADABLE, ascii(text)
    assert block_reason(tool_name="search_docs", arguments={"query": text}) == UNREADABLE, ascii(text)
    assert sql_block_reason(text, sql_context="shared") == UNREADABLE, ascii(text)


def _nest(command, shell, n):
    import shlex

    for _ in range(n):
        command = f"{shell} -c {shlex.quote(command)}"
    return command


@pytest.mark.parametrize("n", [4, 5, 8, 9, 12])
def test_shell_nested_past_the_read_depth_is_refused(n):
    assert shell_block_reason(_nest("rm -rf /", "sh", n))
    assert shell_block_reason(_nest("ls -la", "bash", n)) == "could not read the command safely"


@pytest.mark.parametrize("n", [1, 2, 3])
def test_ordinary_nested_shells_pass(n):
    assert shell_block_reason(_nest("ls -la /tmp", "sh", n)) is None


# ── blocklist: which arguments hold SQL ─────────────────────────────────────

_SQL_TOOLS = ["postgres_query", "run_snowflake_query", "write_query", "read_query", "bigquery_query",
              "duckdb_query", "start_query_execution"]


@pytest.mark.parametrize("tool", _SQL_TOOLS)
@pytest.mark.parametrize("text", ["TRUNCATE orders", "DELETE orders"])
def test_a_tool_named_for_a_sql_engine_gets_the_full_sql_rules(tool, text):
    assert block_reason(tool_name=tool, arguments={"query": text}), (tool, text)


@pytest.mark.parametrize("tool,arguments", [
    ("execute_sql", {"query_text": "DELETE FROM orders"}),
    ("start_query_execution", {"QueryString": "DELETE FROM orders"}),
    ("run_query", {"queries": ["SELECT 1", "DELETE FROM orders"]}),
    # Spellings only the "sql" rules refuse (the shared rules pass them).
    ("execute_sql", {"query_text": "DELETE orders"}),
    ("start_query_execution", {"QueryString": "TRUNCATE orders"}),
    ("run_query", {"queries": ["SELECT 1", "DELETE orders"]}),
    ("postgres_query", {"sql_query": "SELECT 1", "statements": ["SELECT 1", "TRUNCATE orders"]}),
    # Other text on a SQL tool gets the shared rules.
    ("postgres_query", {"database": "x; DROP SCHEMA s"}),
])
def test_sql_keys_on_a_sql_tool_get_the_sql_rules(tool, arguments):
    assert block_reason(tool_name=tool, arguments=arguments), (tool, arguments)


@pytest.mark.parametrize("tool", ["search_docs", "query_knowledge_base", "search", "search_query",
                                  "query_docs", "semantic_search"])
@pytest.mark.parametrize("text", ["delete account", "truncate text", "delete branch"])
def test_search_tools_stay_lenient_under_the_wider_name_rule(tool, text):
    assert block_reason(tool_name=tool, arguments={"query": text}) is None, tool
    assert block_reason(tool_name=tool, arguments={"queries": [text]}) is None, tool


# Search, natural-language and history tools named for a SQL engine: their
# "query" is a question or a filter, not SQL.
_ENGINE_SEARCH_TOOLS = ["snowflake_cortex_search_query", "databricks_genie_query", "postgres_natural_language_query",
                        "pg_query_history", "athena_list_named_queries", "search_snowflake_queries",
                        "bigquery_search_saved_queries", "read_query_logs", "snowflake_semantic_query",
                        "redshift_vector_query", "snowflake_analyst_query", "ask_postgres_query",
                        # Words between the engine and "query": not a SQL tool by name.
                        "snowflake_cortex_complete_query", "databricks_ai_query"]


@pytest.mark.parametrize("tool", _ENGINE_SEARCH_TOOLS)
@pytest.mark.parametrize("text", ["how do I delete a user", "delete account", "truncate text"])
def test_search_tools_named_for_a_sql_engine_stay_lenient(tool, text):
    assert block_reason(tool_name=tool, arguments={"query": text}) is None, tool


@pytest.mark.parametrize("tool", _ENGINE_SEARCH_TOOLS)
def test_sql_sent_to_a_search_tool_named_for_an_engine_still_gets_the_shared_rules(tool):
    assert block_reason(tool_name=tool, arguments={"query": "DELETE FROM orders"}) == DELETE


@pytest.mark.parametrize("tool", ["postgres_raw_query", "pg_safe_query", "snowflake_readonly_query",
                                  "trino_adhoc_query", "bigquery_run_readonly_query", "postgres_readonly_query",
                                  "mssql_query"])
@pytest.mark.parametrize("text", ["TRUNCATE orders", "DELETE orders", "SET XACT_ABORT ON DELETE FROM orders"])
def test_a_tool_with_words_between_engine_and_query_gets_the_full_sql_rules(tool, text):
    assert block_reason(tool_name=tool, arguments={"query": text}), (tool, text)


@pytest.mark.parametrize("tool", ["runSQLLogQuery", "MSSQLListQuery", "run_sql_log_query", "mysql_list_query",
                                  "PostgreSQLHistoryQuery", "sqlite_saved_query", "TSQLSearch"])
@pytest.mark.parametrize("text", ["DELETE FROM x", "TRUNCATE TABLE x"])
def test_guard_words_never_demote_a_name_that_names_sql(tool, text):
    # log, list, history, search … demote engine-named search tools, never a
    # name with sql (or tsql, mssql, mysql, postgresql, sqlite) as a part.
    assert block_reason(tool_name=tool, arguments={"input": text}), (tool, text)


@pytest.mark.parametrize("tool", ["snowflake_cortex_query", "postgres_prompt_query", "bigquery_ai_complete_query",
                                  "databricks_complete_query"])
def test_ai_tools_named_for_a_sql_engine_stay_lenient(tool):
    assert block_reason(tool_name=tool, arguments={"query": "delete account"}) is None, tool


@pytest.mark.parametrize("tool", ["mssql_ai_query", "execute_sql_prompt", "run_query_complete",
                                  "tsql_cortex_execute", "mysql_complete_query", "execute_query_ai"])
def test_ai_words_never_demote_a_name_that_says_sql_otherwise(tool):
    # The AI words (complete, ai, cortex, prompt) apply only to a name that is
    # a SQL tool by engine + query alone.
    assert block_reason(tool_name=tool, arguments={"query": "DELETE orders"}) == DELETE, tool


@pytest.mark.parametrize("tool", ["PostgresRawQuery", "postgresRawQuery", "snowflakeReadonlyQuery",
                                  "MssqlQuery", "runQuery", "executeSql", "PGQuery"])
@pytest.mark.parametrize("text", ["TRUNCATE orders", "SET XACT_ABORT ON DELETE FROM orders"])
def test_a_camel_case_sql_tool_name_gets_the_full_sql_rules(tool, text):
    assert block_reason(tool_name=tool, arguments={"query": text}), (tool, text)


@pytest.mark.parametrize("tool", ["snowflakeCortexSearchQuery", "searchDocs", "SearchDocs", "queryKnowledgeBase",
                                  "databricksAiQuery", "pgQueryHistory", "snowflakeCortexCompleteQuery"])
@pytest.mark.parametrize("text", ["delete account", "truncate text", "TRUNCATE orders"])
def test_camel_case_search_tools_stay_lenient(tool, text):
    assert block_reason(tool_name=tool, arguments={"query": text}) is None, (tool, text)


@pytest.mark.parametrize("tool", _SQL_TOOLS + ["execute_sql", "run_query"])
def test_every_sql_tool_of_the_last_round_stays_classified(tool):
    assert block_reason(tool_name=tool, arguments={"query": "DELETE orders"}) == DELETE, tool


@pytest.mark.parametrize("description", [
    "The SQL query to run", "SQL statement", "A T-SQL batch", "Statement to execute against the warehouse",
])
def test_a_parameter_described_as_sql_gets_the_sql_rules(description):
    schema = {"type": "object", "properties": {"text": {"type": "string", "description": description}}}
    assert block_reason(tool_name="lookup", arguments={"text": "DELETE FROM orders"}, input_schema=schema) == DELETE
    nested = {"type": "object", "properties": {"opts": {"type": "object", "properties": {
        "body": {"type": "array", "items": {"type": "string"}, "title": description}}}}}
    assert block_reason(tool_name="lookup", arguments={"opts": {"body": ["SELECT 1", "TRUNCATE orders"]}},
                        input_schema=nested) == TRUNCATE


@pytest.mark.parametrize("schema", [
    None, {}, {"type": "object"}, {"properties": "junk"},
    {"type": "object", "properties": {"text": {"type": "string", "description": "Search text"}}},
    {"type": "object", "properties": {"text": {"type": "string", "description": "Bank statement id"}}},
])
def test_a_parameter_not_described_as_sql_keeps_its_key_rule(schema):
    assert block_reason(tool_name="lookup", arguments={"text": "DELETE FROM orders"}, input_schema=schema) is None


@pytest.mark.parametrize("prop,defs", [
    ({"$ref": "#/$defs/Sql"}, {"$defs": {"Sql": {"type": "string", "description": "SQL to run"}}}),
    ({"$ref": "#/definitions/Sql"}, {"definitions": {"Sql": {"type": "string", "title": "SQL statement"}}}),
    # A reference to a reference, and the shapes pydantic writes.
    ({"$ref": "#/$defs/A"}, {"$defs": {"A": {"$ref": "#/$defs/Sql"}, "Sql": {"description": "SQL to run"}}}),
    ({"allOf": [{"$ref": "#/definitions/Sql"}]}, {"definitions": {"Sql": {"description": "SQL to run"}}}),
    ({"anyOf": [{"$ref": "#/$defs/Sql"}, {"type": "null"}]}, {"$defs": {"Sql": {"description": "SQL to run"}}}),
])
def test_a_parameter_whose_schema_reference_is_described_as_sql_gets_the_sql_rules(prop, defs):
    schema = {"type": "object", "properties": {"body": prop}, **defs}
    assert block_reason(tool_name="warehouse", arguments={"body": "DELETE FROM orders"}, input_schema=schema) == DELETE


@pytest.mark.parametrize("defs", [
    {"$defs": {"A": {"$ref": "#/$defs/A"}}},  # a reference to itself
    {"$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}}},  # a cycle
    {"$defs": {}},  # a dangling reference
])
def test_a_schema_reference_that_never_resolves_keeps_the_key_rule(defs):
    schema = {"type": "object", "properties": {"body": {"$ref": "#/$defs/A"}}, **defs}
    assert block_reason(tool_name="warehouse", arguments={"body": "DELETE FROM orders"}, input_schema=schema) is None
    assert block_reason(tool_name="warehouse", arguments={"body": "DROP TABLE orders"}, input_schema=schema) == DROP


def _deep_schema(levels):
    schema = {"type": "object", "properties": {"note": {"type": "string", "description": "A note"}}}
    for _ in range(levels):
        schema = {"type": "object", "properties": {"w": schema}}
    return schema


def test_a_schema_nested_past_the_read_depth_reads_every_argument_as_sql():
    # Fail closed: a schema too deep to read could describe any parameter as
    # SQL, so every argument of the tool gets the "sql" rules (the strictest).
    deep = _deep_schema(40)
    for arguments in ({"note": "DELETE FROM orders"}, {"title": "TRUNCATE orders"}):
        assert block_reason(tool_name="warehouse", arguments=arguments, input_schema=deep), arguments
    assert block_reason(tool_name="warehouse", arguments={"note": "select 1"}, input_schema=deep) is None
    # Accepted false refusal (new, named in the PR): prose under such a tool.
    assert block_reason(tool_name="warehouse", arguments={"note": "delete account"}, input_schema=deep) == DELETE
    # A schema within the read depth keeps the key rule.
    assert block_reason(tool_name="warehouse", arguments={"note": "DELETE FROM orders"},
                        input_schema=_deep_schema(20)) is None


@pytest.mark.timeout(60)
@pytest.mark.parametrize("filler", ["statement " * 64_000, "run " * 160_000, "execute statements " * 32_000])
def test_a_long_schema_description_is_read_in_linear_time(filler):
    # A hang guard, not a timing assert: the description regex was quadratic
    # on MCP server text, about 11 s for 160 KB, so these 600+ KB texts would
    # take minutes; a linear read takes well under a second.
    schema = {"type": "object", "properties": {"text": {"type": "string", "description": filler,
                                                       "title": filler}}}
    expect = DELETE if "execute statements" in filler else None  # the parameter is described as SQL
    assert block_reason(tool_name="lookup", arguments={"text": "DELETE FROM t"}, input_schema=schema) == expect


@pytest.mark.parametrize("description,expect", [
    ("Run this statement", DELETE), ("Statements to execute", DELETE), ("The T-SQL batch", DELETE),
    ("Bank statement id", None), ("Run the job", None),
    # The whole title and description are read, however long.
    ("A " + "long text " * 400 + "SQL to run", DELETE), ("x" * 2200 + " Pass the SQL statement to run.", DELETE),
    ("y " * 1100 + "Body of the T-SQL batch to execute", DELETE), ("z " * 1100 + "Statements to run", DELETE),
])
def test_a_description_is_read_by_words(description, expect):
    schema = {"type": "object", "properties": {"text": {"type": "string", "description": description}}}
    assert block_reason(tool_name="lookup", arguments={"text": "DELETE FROM orders"}, input_schema=schema) == expect
    titled = {"type": "object", "properties": {"text": {"type": "string", "title": description}}}
    assert block_reason(tool_name="lookup", arguments={"text": "DELETE FROM orders"}, input_schema=titled) == expect


def _ref_chain(hops, description):
    defs = {f"D{i}": {"$ref": f"#/$defs/D{i + 1}"} for i in range(hops - 1)}
    defs[f"D{hops - 1}"] = {"type": "string", "description": description}
    return {"type": "object", "properties": {"body": {"$ref": "#/$defs/D0"}}, "$defs": defs}


@pytest.mark.parametrize("hops", [3, 8, 10, 40])
def test_a_long_schema_reference_chain_counts_as_sql(hops):
    # Within the hop bound the chain's end is read; past it the parameter is
    # read as SQL (fail closed: the unread end could say so).
    schema = _ref_chain(hops, "SQL to run")
    assert block_reason(tool_name="warehouse", arguments={"body": "TRUNCATE orders"}, input_schema=schema) == TRUNCATE


def test_a_reference_chain_past_the_hop_bound_is_read_as_sql_whatever_it_ends_in():
    # Accepted false refusal (new): the end of the chain is never read.
    schema = _ref_chain(10, "Search text")
    assert block_reason(tool_name="warehouse", arguments={"body": "delete account"}, input_schema=schema) == DELETE
    short = _ref_chain(3, "Search text")
    assert block_reason(tool_name="warehouse", arguments={"body": "delete account"}, input_schema=short) is None


@pytest.mark.parametrize("items", [
    {"type": "string", "description": "One T-SQL statement to run"},
    {"type": "string", "title": "SQL statement"},
    {"$ref": "#/$defs/Stmt"},
    {"anyOf": [{"$ref": "#/$defs/Stmt"}, {"type": "null"}]},
])
def test_array_items_described_as_sql_get_the_sql_rules(items):
    schema = {"properties": {"batch": {"type": "array", "items": items}},
              "$defs": {"Stmt": {"type": "string", "description": "SQL to run"}}}
    assert block_reason(tool_name="warehouse", arguments={"batch": ["TRUNCATE orders"]},
                        input_schema=schema) == TRUNCATE
    by_ref = {"properties": {"batch": {"$ref": "#/$defs/Batch"}},
              "$defs": {"Batch": {"type": "array", "items": items},
                        "Stmt": {"type": "string", "description": "SQL to run"}}}
    assert block_reason(tool_name="warehouse", arguments={"batch": ["TRUNCATE orders"]},
                        input_schema=by_ref) == TRUNCATE


def test_a_parameter_with_more_schema_nodes_than_the_read_bound_is_read_as_sql():
    # Fail closed: past 256 nodes for one parameter the rest is not read.
    def schema(k):
        return {"properties": {"body": {"anyOf": [{"type": "string", "description": "Text %d" % i}
                                                  for i in range(k)]}}}

    assert block_reason(tool_name="warehouse", arguments={"body": "delete account"}, input_schema=schema(200)) is None
    assert block_reason(tool_name="warehouse", arguments={"body": "delete account"},
                        input_schema=schema(300)) == DELETE


def test_array_items_not_described_as_sql_keep_the_key_rule():
    schema = {"properties": {"tags": {"type": "array", "items": {"type": "string", "description": "A tag"}}}}
    assert block_reason(tool_name="warehouse", arguments={"tags": ["TRUNCATE orders"]}, input_schema=schema) is None


# ── small-model judge ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    '{"approve": "false", "confidence": 0.99}', '{"approve": "no", "confidence": 0.99}',
    '{"approve": 0, "confidence": 0.99}', '{"approve": "true", "confidence": 0.99}',
    '{"approve": 1, "confidence": 0.99}', '{"confidence": 0.99}',
])
def test_a_judge_answer_without_a_real_true_never_allows(raw):
    from app.ai.classifiers.tool_call_judge import ToolCallVerdict

    v = ToolCallVerdict.parse(raw)
    assert v.approve is False
    assert pm.verdict_from_small_model(v.approve, v.confidence, _settings()) == "ask"


def test_a_judge_answer_with_true_approves():
    from app.ai.classifiers.tool_call_judge import ToolCallVerdict

    v = ToolCallVerdict.parse('```json\n{"approve": true, "confidence": 0.95, "reason": "read only"}\n```')
    assert (v.approve, v.confidence) == (True, 0.95)
    assert pm.verdict_from_small_model(v.approve, v.confidence, _settings()) == "allow"


# ── mode resolution ─────────────────────────────────────────────────────────

def _settings(**kw):
    return pm.PermissionSettings(**{**pm.normalize_settings({}), **kw})


def test_allow_all_is_refused_unless_explicitly_permitted():
    assert pm.effective_mode("allow_all", _settings(allow_all_enabled=True)) == "ask"


def test_report_mode_wins_over_the_org_default():
    s = _settings(default_mode="auto")
    assert pm.effective_mode(None, s) == "auto"
    assert pm.effective_mode("ask", s) == "ask"


@pytest.mark.parametrize("switch,permitted,expect", [
    (True, True, "allow_all"), (False, True, "ask"), (True, False, "ask"),
])
def test_allow_all_needs_switch_and_a_watched_owner_run(switch, permitted, expect):
    s = _settings(allow_all_enabled=switch)
    assert pm.effective_mode("allow_all", s, allow_all_permitted=permitted) == expect


def test_strictest_agent_cap_applies():
    s = _settings(allow_all_enabled=True)
    assert pm.effective_mode("allow_all", s, agent_caps=["auto"], allow_all_permitted=True) == "auto"
    assert pm.effective_mode("allow_all", s, agent_caps=["auto", "ask", None], allow_all_permitted=True) == "ask"
    assert pm.effective_mode("ask", s, agent_caps=["auto"]) == "ask"  # a cap never loosens


@pytest.mark.parametrize("p,expect", [
    (0.99, "allow"), (0.85, "allow"), (0.5, "ask"), (0.3, "refuse"), (0.0, "refuse"),
    (None, "ask"), (float("nan"), "ask"), ("junk", "ask"),
])
def test_decision_model_probability_maps_to_three_outcomes(p, expect):
    assert pm.verdict_from_probability(p, _settings()) == expect


@pytest.mark.parametrize("approve,conf,expect", [
    (True, 0.9, "allow"), (False, 0.9, "refuse"), (True, 0.5, "ask"), (False, 0.0, "ask"),
])
def test_small_model_acts_only_on_a_confident_answer(approve, conf, expect):
    assert pm.verdict_from_small_model(approve, conf, _settings()) == expect


def test_an_unreadable_stored_setting_falls_back_to_the_defaults_and_is_logged(caplog):
    class Broken:
        def get_config(self, key):
            raise ValueError("corrupt JSON")

    with caplog.at_level("WARNING", logger="app.services.tool_permission_mode"):
        s = pm.permission_settings(Broken())
    assert (s.default_mode, s.allow_all_enabled) == ("ask", False)
    assert any("corrupt JSON" in r.getMessage() for r in caplog.records)


def test_settings_normalize_bad_values_to_safe_defaults():
    s = pm.normalize_settings({
        "default_mode": "allow_all", "allow_all_enabled": "yes", "unattended": "sure",
        "allow_threshold": 0.4, "refuse_threshold": 0.9, "junk": 1,
    })
    assert s["default_mode"] == "ask"          # allow_all needs a real True switch
    assert s["allow_all_enabled"] is False
    assert s["unattended"] == "refuse"
    assert s["allow_threshold"] >= pm.MIN_ALLOW_THRESHOLD
    assert s["refuse_threshold"] <= s["allow_threshold"]
    enabled = pm.normalize_settings({"default_mode": "allow_all", "allow_all_enabled": True})
    assert enabled["default_mode"] == "allow_all"  # the default while the switch is on
    assert "junk" not in s
    assert pm.normalize_settings({"judge_decision_model_id": "small"})["judge_decision_model_id"] == "small"


@pytest.mark.parametrize("command,windows,expect", [
    ("ls -la /var/log", False, True), ("df -h", False, True),
    ("ps aux", False, False), ("ps eww", False, False),  # can print other processes' environment
    ("Get-ChildItem Env:", True, False), ("Get-Item Env:AWS_SECRET_ACCESS_KEY", True, False),
    ("ls; rm x", False, False), ("ls $(whoami)", False, False), ("rm x", False, False),
    ("ls *", False, False), ("ls > f", False, False),
    # Reads contents (could print a secret) or can also set something: judge.
    ("cat ~/.ssh/id_rsa", False, False), ("tail -F app.log", False, False),
    ("date -s 2020-01-01", False, False), ("hostname newname", False, False),
    ("Get-ChildItem C:\\data", True, True), ("dir C:\\", True, True),
    ("Remove-Item C:\\x", True, False), ("Get-Content .env", True, False),
    ("ipconfig /release", True, False),
])
def test_read_only_shell_rule(command, windows, expect):
    assert pm.is_read_only_shell_command(command, windows=windows) is expect


@pytest.mark.parametrize("trigger,can_prompt,grants,machine", [
    # Machine turns shown in the report: a person sees the card, as on main.
    ("wait", True, True, False), ("checkin", True, True, False),
    ("sub_agent_message", True, True, False), ("sub_agent_wave", True, True, False),
    # A sub-agent's hidden turn: the report's grants apply, a prompt cannot.
    ("sub_agent", False, True, True),
    # Eval runs and any value this code does not know: unattended.
    ("eval_run", False, False, True), ("something_new", False, False, True),
])
def test_machine_turns_split_answering_a_card_from_the_reports_grants(trigger, can_prompt, grants, machine):
    from app.services.tool_policy_service import ToolPolicyService

    head = SimpleNamespace(webhook_id=None, trigger_source=trigger, scheduled_prompt_id=None, started_via=None)
    ctx = {"user": SimpleNamespace(id="u"), "head_completion": head}
    assert ToolPolicyService.is_interactive_run(ctx) is can_prompt
    assert ToolPolicyService.report_grants_apply(ctx) is grants
    assert ToolPolicyService.is_machine_run(ctx) is machine


def test_scheduled_webhook_api_and_other_channel_runs_get_no_report_grants():
    from app.services.tool_policy_service import ToolPolicyService

    user = SimpleNamespace(id="u")
    blank = {"webhook_id": None, "trigger_source": None, "scheduled_prompt_id": None, "started_via": None}
    for marker, value in (("webhook_id", "w"), ("scheduled_prompt_id", "s"), ("started_via", "api_key"),
                          ("started_via", "unattended")):
        # A machine turn of such a run is unattended too, whatever its trigger.
        for trigger in (None, "sub_agent", "wait", "checkin", "sub_agent_wave", "sub_agent_message"):
            head = SimpleNamespace(**{**blank, marker: value, "trigger_source": trigger})
            ctx = {"user": user, "head_completion": head}
            assert not ToolPolicyService.report_grants_apply(ctx)
            assert not ToolPolicyService.is_interactive_run(ctx)
            assert ToolPolicyService.is_machine_run(ctx)
    assert not ToolPolicyService.report_grants_apply({"user": user, "platform": "slack"})
    assert not ToolPolicyService.report_grants_apply({"user": user, "is_eval_run": True})
    assert ToolPolicyService.report_grants_apply({"user": user, "head_completion": SimpleNamespace(**blank)})


@pytest.mark.parametrize("trigger", [None, "wait", "checkin", "sub_agent", "sub_agent_wave", "sub_agent_message"])
def test_a_platform_run_is_unwatched_and_gets_no_report_grants_as_on_main(trigger):
    # As on main (4b99ad3de is_interactive_run): a Slack/Teams/email run, with
    # or without a trigger, shows no card and gets no report grants. It is a
    # person's run, so the unattended 'allow' does not cover it either.
    from app.services.tool_policy_service import ToolPolicyService

    blank = {"webhook_id": None, "trigger_source": trigger, "scheduled_prompt_id": None, "started_via": None}
    ctx = {"user": SimpleNamespace(id="u"), "platform": "slack", "head_completion": SimpleNamespace(**blank)}
    assert not ToolPolicyService.is_interactive_run(ctx)
    assert not ToolPolicyService.report_grants_apply(ctx)
    assert not ToolPolicyService.is_machine_run(ctx)
    # A scheduled run that answers on a platform is still a job.
    ctx["head_completion"] = SimpleNamespace(**{**blank, "scheduled_prompt_id": "s"})
    assert ToolPolicyService.is_machine_run(ctx) and not ToolPolicyService.is_interactive_run(ctx)


@pytest.mark.parametrize("trigger", ["wait", "checkin", "sub_agent", "sub_agent_wave", "sub_agent_message"])
def test_a_turn_started_by_a_platform_run_is_a_platform_run_too(trigger):
    # Its head carries the 'channel' mark and no platform of its own: the same
    # rules as the Slack/Teams/email turn that started it.
    from app.services.tool_policy_service import ToolPolicyService

    head = SimpleNamespace(webhook_id=None, trigger_source=trigger, scheduled_prompt_id=None, started_via="channel")
    ctx = {"user": SimpleNamespace(id="u"), "head_completion": head}
    assert not ToolPolicyService.is_interactive_run(ctx)
    assert not ToolPolicyService.report_grants_apply(ctx)
    assert not ToolPolicyService.is_machine_run(ctx)  # a person's run: the unattended 'allow' does not cover it


def _origin_mark(rows, completion_id):
    """unattended_origin over a session that holds only ``rows`` (id -> row)."""
    import asyncio

    from app.services.machine_turn import unattended_origin

    class Rows:
        async def get(self, model, key):
            return rows.get(key)

    return asyncio.run(unattended_origin(Rows(), completion_id))


def _completion_row(role, parent_id=None, **fields):
    blank = {"started_via": None, "scheduled_prompt_id": None, "webhook_id": None, "prompt": None,
             "external_platform": None, "trigger_source": None}
    return SimpleNamespace(role=role, parent_id=parent_id, **{**blank, **fields})


def test_an_origin_whose_head_row_is_gone_is_unattended(caplog):
    # The system row names its head, and only the head says how the turn was
    # started (API key, schedule, channel): with the head missing nobody can
    # say a person watched it, whatever the system row's own fields are.
    system = _completion_row("system", parent_id="head")
    with caplog.at_level("WARNING", logger="app.services.machine_turn"):
        assert _origin_mark({"system": system}, "system") == "unattended"
    assert "head" in caplog.text and "unattended" in caplog.text
    # Controls: the same system row with its head there follows the head.
    assert _origin_mark({"system": system, "head": _completion_row("user")}, "system") is None
    assert _origin_mark({"system": system, "head": _completion_row("user", started_via="api_key")},
                        "system") == "api_key"
    # A row that names no head is read by its own fields, as before.
    assert _origin_mark({"system": _completion_row("system")}, "system") is None
    assert _origin_mark({"head": _completion_row("user")}, "head") is None


def test_webhook_turn_counts_as_unattended():
    from types import SimpleNamespace

    from app.services.tool_policy_service import ToolPolicyService

    user = SimpleNamespace(id="u")
    blank = {"webhook_id": None, "trigger_source": None, "scheduled_prompt_id": None, "started_via": None}
    for marker in ("webhook_id", "trigger_source", "scheduled_prompt_id"):
        head = SimpleNamespace(**{**blank, marker: "x"})
        assert not ToolPolicyService.is_interactive_run({"user": user, "head_completion": head})
    # A run started with an API key: nobody is in the loop to answer.
    head = SimpleNamespace(**{**blank, "started_via": "api_key"})
    assert not ToolPolicyService.is_interactive_run({"user": user, "head_completion": head})
    assert ToolPolicyService.is_machine_run({"user": user, "head_completion": head})
    head = SimpleNamespace(**blank)
    assert ToolPolicyService.is_interactive_run({"user": user, "head_completion": head})
    assert not ToolPolicyService.is_machine_run({"user": user, "head_completion": head})
