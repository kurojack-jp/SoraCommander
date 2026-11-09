"""Which Git requests are refused. Pure text checks: nothing runs and no window opens."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.git_push_guard import push_refused

GIT = r"C:\Program Files\Git\cmd\git.exe"
WORK = "HEAD:refs/heads/sora/g2-protected-broker-20261028"


class AllowedTests(unittest.TestCase):
    def test_the_one_explicit_form_is_left_to_owner_review(self):
        for refspec in (WORK, "abc123:refs/heads/feature", "sora/x:refs/heads/sora/x",
                        "HEAD:refs/heads/main-notes", "HEAD:refs/heads/sora/main", "HEAD:refs/heads/mainline",
                        "HEAD:refs/heads/feature+fix", "a+b:refs/heads/c+d"):
            with self.subTest(refspec=refspec):
                self.assertFalse(push_refused(GIT, ("push", "origin", refspec)))

    def test_other_git_commands_have_no_opinion_here(self):
        for argv in (("status", "--short"), ("add", "--", "a.txt"), ("commit", "-m", "push to main", "--", "a.txt"),
                     ("log", "-1"), ("diff", "main"), ("--version",),
                     ("-c", "core.quotepath=false", "status"), ("-c", "color.ui=never", "log"),
                     ("--no-pager", "log"), ("-P", "diff"), ("--no-optional-locks", "status"), ()):
            with self.subTest(argv=argv):
                self.assertFalse(push_refused(GIT, argv))

    def test_ordinary_commands_that_mention_push_stay_reviewable(self):
        for argv in (("log", "--grep", "push"), ("commit", "-m", "git push", "--", "a.txt")):
            with self.subTest(argv=argv):
                self.assertFalse(push_refused(GIT, argv))

    def test_reading_configuration_stays_reviewable(self):
        for argv in (("config", "--get", "remote.origin.url"), ("config", "--get-all", "push.default"),
                     ("config", "--get-regexp", "^alias"), ("config", "--list"), ("config", "-l", "--show-origin"),
                     ("config", "--global", "--get", "user.name"), ("config", "--local", "--list", "--name-only"),
                     ("config", "user.name"), ("config", "get", "user.name"), ("config", "list")):
            with self.subTest(argv=argv):
                self.assertFalse(push_refused(GIT, argv))

    def test_the_named_forms_stay_reviewable(self):
        for argv in (("rebase", "origin/feature"), ("rebase", "--continue"), ("rebase", "--abort"), ("rebase", "--skip"),
                     ("rebase", "--onto", "a", "b"), ("rebase", "--onto=a", "b"), ("rebase", "--autostash", "x"),
                     ("bisect",), ("bisect", "start"), ("bisect", "good", "x"), ("bisect", "bad"), ("bisect", "reset"),
                     ("submodule",), ("submodule", "status"), ("submodule", "--quiet", "status", "--recursive"),
                     ("submodule", "summary"), ("remote",), ("remote", "-v"), ("remote", "--verbose"),
                     ("remote", "show", "origin"), ("remote", "get-url", "origin")):
            with self.subTest(argv=argv):
                self.assertFalse(push_refused(GIT, argv))

    def test_similar_looking_ordinary_options_stay_reviewable(self):
        for argv in (("log", "--oneline"), ("log", "--output-indicator-new=x"), ("log", "-O", "order.txt"),
                     ("format-patch", "--output-directory", "x", "HEAD~1"), ("fetch", "origin"), ("fetch", "--prune"),
                     ("pull", "--ff-only", "origin", "x"), ("ls-remote", "origin"), ("grep", "-n", "x"),
                     ("grep", "-e", "FOO"), ("grep", "--count", "x"), ("show", "--stat")):
            with self.subTest(argv=argv):
                self.assertFalse(push_refused(GIT, argv))

    def test_other_programs_have_no_opinion_here(self):
        for executable, argv in ((sys.executable, ("-c", "print('git push')")), (r"C:\x\gh.exe", ("pr", "list")),
                                 (r"C:\x\legit.exe", ("push", "origin", "main"))):
            with self.subTest(executable=executable):
                self.assertFalse(push_refused(executable, argv))

    def test_a_shell_without_a_git_push_has_no_opinion(self):
        for argv in (("/c", "dir"), ("-Command", "git status"), ("-c", "echo push")):
            with self.subTest(argv=argv):
                self.assertFalse(push_refused(r"C:\Windows\System32\cmd.exe", argv))


class RefusedTests(unittest.TestCase):
    def assert_refused(self, *cases):
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertTrue(push_refused(GIT, argv))

    def test_protected_branches_in_any_case(self):
        self.assert_refused(*(("push", "origin", f"HEAD:refs/heads/{name}")
                              for name in ("main", "master", "MAIN", "Main", "MASTER")))

    def test_target_not_explicit(self):
        self.assert_refused(("push",), ("push", "origin"), ("push", "origin", "HEAD"), ("push", "origin", "main"),
                            ("push", "origin", "HEAD:main"), ("push", "origin", "HEAD:feature"),
                            ("push", "origin", "refs/heads/main"), ("Push", "origin", "main"))

    def test_history_rewrite_and_deletion(self):
        self.assert_refused(("push", "origin", "+HEAD:refs/heads/feature"), ("push", "-f", "origin", WORK),
                            ("push", "--force", "origin", WORK), ("push", "--force-with-lease", "origin", WORK),
                            ("push", "origin", ":refs/heads/feature"), ("push", "--delete", "origin", "feature"),
                            ("push", "-d", "origin", "feature"))

    def test_many_refs(self):
        self.assert_refused(("push", "--all", "origin"), ("push", "--mirror", "origin"), ("push", "--tags", "origin"),
                            ("push", "--prune", "origin", WORK), ("push", "origin", WORK, "HEAD:refs/heads/other"),
                            ("push", "origin", "refs/heads/*:refs/heads/*"), ("push", "origin", "HEAD:refs/heads/*"))

    def test_other_remote(self):
        self.assert_refused(("push", "upstream", WORK), ("push", "https://github.com/x/y.git", WORK),
                            ("push", r"D:\other", WORK), ("push", "ORIGIN", WORK))

    def test_any_option_including_hook_bypass(self):
        self.assert_refused(("push", "--no-verify", "origin", WORK), ("push", "origin", WORK, "--no-verify"),
                            ("push", "--dry-run", "origin", WORK), ("push", "-u", "origin", WORK),
                            ("push", "--receive-pack=x", "origin", WORK), ("push", "-o", "x", "origin", WORK))

    def test_global_options_before_push(self):
        self.assert_refused(("-C", r"D:\other", "push", "origin", WORK), ("-c", "core.hooksPath=x", "push", "origin", WORK),
                            ("--git-dir", r"D:\o\.git", "push", "origin", WORK), ("--git-dir=x", "push", "origin", WORK),
                            ("--work-tree", "x", "push", "origin", WORK), ("--no-pager", "push", "origin", WORK),
                            ("--exec-path", "x", "push", "origin", WORK), ("--config-env", "x=y", "push", "origin", WORK))

    def test_alias_definitions_for_any_subcommand(self):
        self.assert_refused(("-c", "alias.p=push", "p", "origin", "main"), ("-c", "ALIAS.p=push", "p"),
                            ("-c", "alias.st=status", "st"), ("--config-env", "alias.p=X", "p"),
                            ("--config-env=alias.p=X", "p"),
                            # A listed command, so only the alias definition itself refuses these.
                            ("-c", "alias.x=push", "status"), ("-c", "ALIAS.x=push", "status"),
                            ("--config-env", "alias.x=Y", "status"), ("--config-env=alias.x=Y", "status"),
                            ("--CONFIG-ENV=ALIAS.x=Y", "status"))

    def test_joined_option_values(self):
        self.assert_refused(("-calias.p=push", "p"), ("-cALIAS.p=push", "status"), ("-ccore.hooksPath=x", "push", "origin", WORK))

    def test_a_saved_alias_or_unknown_command_is_refused(self):
        self.assert_refused(("p",), ("p", "origin", "main"), ("st",), ("my-tool", "x"), ("Status",), ("PUSH", "origin", "main"))

    def test_commands_that_can_send_without_the_word_push(self):
        self.assert_refused(("send-pack", "origin", "main"), ("http-push", "x"), ("filter-branch", "x"), ("bundle", "x"),
                            ("fast-export", "x"), ("subtree", "push", "x"), ("lfs", "push", "x"))

    def test_commands_this_project_forbids_are_not_reviewable(self):
        self.assert_refused(("clone", "x"), ("worktree", "add", "x"), ("init",))

    def test_commands_that_run_other_commands(self):
        self.assert_refused(("rebase", "-x", "git push origin main"), ("rebase", "--exec", "git PUSH"),
                            ("submodule", "foreach", "git push"), ("bisect", "run", "sh", "-c", "git push"),
                            # No word push anywhere, because the program is named by the request alone.
                            ("rebase", "--exec", "x.bat"), ("rebase", "--exec=x.bat"), ("rebase", "-x", "x.bat"),
                            ("rebase", "-xx.bat"), ("rebase", "-ix", "x.bat"), ("rebase", "--exe", "x.bat"),
                            ("rebase", "-i", "x"), ("rebase", "--interactive", "x"), ("rebase", "--strategy=x"),
                            ("bisect", "run", "x.bat"), ("bisect", "visualize"), ("bisect", "replay", "x"),
                            ("submodule", "foreach", "x.bat"), ("submodule", "--quiet", "foreach", "x.bat"),
                            ("submodule", "update"))

    def test_options_that_name_a_program(self):
        self.assert_refused(("fetch", "--upload-pack=x", "origin"), ("fetch", "--upload-pack", "x", "origin"),
                            ("fetch", "--upload=x", "origin"), ("pull", "--upload-pack=x"),
                            ("ls-remote", "--upload-pack=x", "."), ("ls-remote", "--exec=x", "."),
                            ("grep", "-Ox", "a"), ("grep", "-O", "a"), ("grep", "-nOx", "a"),
                            ("grep", "--open-files-in-pager=x", "a"), ("grep", "--open=x", "a"),
                            ("archive", "--exec=x", "--remote=y", "HEAD"))

    def test_output_can_overwrite_the_hook_or_configuration(self):
        self.assert_refused(("log", "-1", "--format=x", "--output=.git/hooks/pre-push"), ("diff", "--output=.git/config"),
                            ("show", "--outp=.git/config"), ("diff-tree", "--output", "x", "HEAD"),
                            ("shortlog", "--OUTPUT=x"),
                            # Commands limited to named words must get this check too.
                            ("bisect", "log", "--output=x"), ("bisect", "--output=x"),
                            ("submodule", "status", "--output=x"), ("submodule", "--output=x"),
                            ("remote", "show", "--output=x"), ("remote", "--outp=x"),
                            ("config", "--list", "--output=x"), ("rebase", "--output=x"))

    def test_every_configuration_change(self):
        self.assert_refused(("config", "core.hooksPath", "x"), ("config", "--global", "core.hookspath", "x"),
                            ("config", "alias.p", "push"), ("config", "remote.origin.url", "x"),
                            ("config", "remote.origin.pushurl", "x"), ("config", "url.x.insteadOf", "y"),
                            ("config", "push.recurseSubmodules", "on-demand"), ("config", "core.sshCommand", "x"),
                            ("config", "--unset", "core.hooksPath"), ("config", "user.name", "x"),
                            # Change the configuration without naming a guarded key.
                            ("config", "--edit"), ("config", "-e"), ("config", "edit"), ("config", "--global", "edit"),
                            ("config", "set", "include.path", "x"), ("config", "include.path", "x"),
                            ("config", "includeIf.onbranch:x.path", "y"), ("config", "--add", "a.b", "c"),
                            ("config", "--unset-all", "a.b"), ("config", "--replace-all", "a.b", "c"),
                            ("config", "--rename-section", "a", "b"), ("config", "--remove-section", "a"),
                            ("config", "unset", "a.b"), ("config", "rename-section", "a", "b"),
                            ("config", "remove-section", "a"), ("config", "--file", "x", "a.b", "c"),
                            ("config", "--list", "set"), ("config", "--get", "edit"), ("config",),
                            ("config", "--global", "set", "a.b", "c"), ("config", "--global", "a.b", "c"))

    def test_changing_remotes_and_submodules(self):
        self.assert_refused(("remote", "set-url", "origin", "x"), ("remote", "rename", "a", "origin"),
                            ("remote", "remove", "origin"), ("remote", "rm", "origin"), ("remote", "add", "other", "x"),
                            ("remote", "-v", "add", "other", "x"), ("remote", "set-head", "origin", "x"),
                            ("remote", "set-branches", "origin", "x"), ("remote", "update"), ("remote", "prune", "origin"),
                            ("submodule", "add", "x"), ("submodule", "set-url", "a", "b"),
                            ("submodule", "set-branch", "a"), ("submodule", "init"), ("submodule", "sync"),
                            ("submodule", "deinit", "a"), ("submodule", "absorbgitdirs"))

    def test_options_before_the_command_that_change_its_target_or_run_something(self):
        self.assert_refused(("-C", r"D:\other", "status"), ("-C", "status"), ("--git-dir", "x", "status"), ("--git-dir=x", "status"),
                            ("--work-tree", "x", "status"), ("--work-tree=x", "status"), ("--namespace=x", "status"),
                            ("--exec-path=x", "status"), ("--exec-path", "x", "status"),
                            ("-c", "core.fsmonitor=x.bat", "status"), ("-c", "core.hooksPath=x", "commit", "-m", "a"),
                            ("-c", "core.sshCommand=x", "fetch"), ("-c", "diff.external=x", "diff"),
                            ("-c", "core.pager=x", "log"), ("-c", "include.path=x", "status"), ("-c",),
                            ("-ccore.quotepath=false", "status"), ("--config-env", "core.pager=X", "log"),
                            ("--config-env=core.pager=X", "log"), ("--paginate", "log"))

    def test_other_names_for_the_command(self):
        for executable in (r"C:\x\git-push.exe", r"C:\x\GIT-PUSH", "git-push"):
            with self.subTest(executable=executable):
                self.assertTrue(push_refused(executable, ("origin", WORK)))
        for executable in ("git", "GIT.EXE", r"C:\x\git.cmd", r"C:\x\Git.exe"):
            with self.subTest(executable=executable):
                self.assertTrue(push_refused(executable, ("push", "origin", "main")))

    def test_shell_forms(self):
        for shell, argv in ((r"C:\Windows\System32\cmd.exe", ("/c", "git", "push", "origin", WORK)),
                            ("cmd", ("/c", "git push origin main")), ("powershell", ("-Command", "git push origin main")),
                            ("pwsh.exe", ("-c", '& "git.exe" push')), ("bash", ("-c", "cd x && GIT push")),
                            ("sh", ("-c", "git -C x push origin main"))):
            with self.subTest(shell=shell, argv=argv):
                self.assertTrue(push_refused(shell, argv))


class DiscardTests(unittest.TestCase):
    """Every request here lost uncommitted work or changed staging in a throwaway repository."""

    def assert_refused(self, *cases):
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertTrue(push_refused(GIT, argv))

    def assert_reviewable(self, *cases):
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertFalse(push_refused(GIT, argv))

    def test_reset_and_restore_in_every_form(self):
        self.assert_refused(("reset", "--hard"), ("reset", "--hard", "HEAD~1"), ("reset", "--merge"), ("reset", "--keep"),
                            ("reset", "--soft", "HEAD~1"), ("reset",), ("reset", "HEAD", "--", "a"), ("reset", "--mixed", "HEAD~1"),
                            ("restore", "a"), ("restore", "."), ("restore", "--staged", "a"), ("restore", "-SW", "a"),
                            ("restore", "--staged", "--worktree", "a"), ("restore", "--source=HEAD~1", "a"))

    def test_clean_only_as_a_dry_run(self):
        self.assert_refused(("clean",), ("clean", "-f"), ("clean", "-fd"), ("clean", "-fdx"), ("clean", "--force"), ("clean", "-d"),
                            ("clean", "-n", "-f"), ("clean", "-nf"), ("clean", "--dry-run", "--force"), ("clean", "-i"),
                            ("clean", "--", "-n"))
        self.assert_reviewable(("clean", "-n"), ("clean", "--dry-run"), ("clean", "-n", "-d"), ("clean", "--dry-run", "-d", "-x"),
                               ("clean", "-n", "-X", "--", "a"))

    def test_checkout_only_creates_a_branch(self):
        self.assert_refused(("checkout", "--", "a"), ("checkout", "."), ("checkout", "HEAD", "--", "a"), ("checkout", "-f", "x"),
                            ("checkout", "--force", "x"), ("checkout", "x"), ("checkout",), ("checkout", "-b"), ("checkout", "-B", "x"),
                            ("checkout", "-b", "x", "--", "a"), ("checkout", "-b", "x", "y", "z"), ("checkout", "-b", "-f"), ("checkout", "-fb", "x"))
        self.assert_reviewable(("checkout", "-b", "fresh"), ("checkout", "-b", "fresh", "HEAD~1"))

    def test_switch_keeps_uncommitted_work(self):
        self.assert_refused(("switch", "-f", "x"), ("switch", "--force", "x"), ("switch", "--for", "x"), ("switch", "--discard-changes", "x"),
                            ("switch", "--discard", "x"), ("switch", "x", "-f"), ("switch", "-C", "x"), ("switch", "-c", "x", "-f"), ("switch",),
                            ("switch", "-"), ("switch", "-c"), ("switch", "--orphan", "x"))
        self.assert_reviewable(("switch", "x"), ("switch", "-c", "fresh"), ("switch", "-c", "fresh", "HEAD~1"))

    def test_stash_is_read_only(self):
        self.assert_refused(("stash",), ("stash", "-u"), ("stash", "--include-untracked"), ("stash", "push"), ("stash", "push", "-m", "x"),
                            ("stash", "save", "x"), ("stash", "pop"), ("stash", "apply"), ("stash", "drop"), ("stash", "clear"),
                            ("stash", "branch", "x"), ("stash", "-q", "list"))
        self.assert_reviewable(("stash", "list"), ("stash", "show"), ("stash", "show", "-p"))

    def test_rm_and_mv_never_force(self):
        self.assert_refused(("rm", "-f", "a"), ("rm", "--force", "a"), ("rm", "-rf", "a"), ("rm", "-fr", "a"), ("rm", "a", "-f"), ("rm", "--for", "a"),
                            ("rm", "-f", "a.txt"), ("rm", "--force", "a.txt"), ("rm", "-rf", "a.txt"), ("rm", "a.txt", "-f"),
                            ("rm", "--cached", "a.txt"), ("rm", "-r", "d.txt"), ("rm", "a"), ("rm", "."), ("rm", "--", "*.txt"),
                            ("mv", "-f", "a", "b"), ("mv", "--force", "a", "b"), ("mv", "-fk", "a", "b"), ("mv", "-k", "a", "b"))
        self.assert_reviewable(("rm", "a.txt"), ("rm", "-q", "a.txt"), ("rm", "--", "-f.txt"),
                               ("mv", "a", "b"), ("mv", "-n", "a", "b"), ("mv", "-v", "--", "-f", "b"))

    def test_commit_must_name_its_paths(self):
        self.assert_refused(("commit",), ("commit", "-m", "x"), ("commit", "-m", "x", "--"), ("commit", "-m", "x", "a"),
                            ("commit", "-a", "-m", "x"), ("commit", "-am", "x"), ("commit", "--all", "-m", "x"),
                            ("commit", "--amend", "-m", "x", "--", "a"), ("commit", "--amend", "--no-edit"),
                            ("commit", "-mx", "--", "a"), ("commit", "--message=x", "--", "a"), ("commit", "--no-verify", "-m", "x", "--", "a"),
                            ("commit", "-m", "x", "--amend", "--", "a"), ("commit", "--", "a"), ("commit", "-m"),
                            ("commit", "--", "a.txt"), ("commit", "-m", "x", "a.txt", "b.txt"))
        self.assert_reviewable(("commit", "-m", "x", "--", "a.txt"), ("commit", "-m", "x", "-m", "y", "--", "a.txt", "d/b.md"),
                               ("commit", "-m", "-a", "--", "a.txt"), ("commit", "-m", "x", "--", ".gitattributes"),
                               ("commit", "-m", "x", "--", "d\\b.md"))

    def test_commit_and_add_refuse_what_selects_many_files(self):
        # Measured: each took the Owner's other changes.
        for many in (".", "..", "./", "d", "d/", "d\\", "*.txt", "d/*", "a?.txt", "[ab].txt", ":/", ":(glob)**", ":!a.txt", "Makefile", "...", "d.old/", "d.old\\"):
            with self.subTest(path=many):
                self.assertTrue(push_refused(GIT, ("commit", "-m", "x", "--", many)))
                self.assertTrue(push_refused(GIT, ("add", "--", many)))
                self.assertTrue(push_refused(GIT, ("commit", "-m", "x", "--", "a.txt", many)))
        self.assert_refused(("add",), ("add", "."), ("add", "-A"), ("add", "-u"), ("add", "--all"), ("add", "a.txt"),
                            ("add", "-f", "--", "a.txt"), ("add", "--"), ("add", "-p"))
        self.assert_reviewable(("add", "--", "a.txt"), ("add", "--", "docs/x.md", "d/b.py"))

    def test_apply_only_reports(self):
        self.assert_refused(("apply", "x.patch"), ("apply", "-R", "x.patch"), ("apply", "--reverse", "x.patch"), ("apply", "--cached", "x.patch"),
                            ("apply", "--index", "x.patch"), ("apply", "--check", "--apply", "x.patch"), ("apply", "--check", "-R", "x.patch"),
                            ("apply",), ("apply", "--", "--check"), ("am", "x.patch"), ("am", "--abort"))
        self.assert_reviewable(("apply", "--check", "x.patch"), ("apply", "--stat", "x.patch"), ("apply", "--numstat", "x.patch"),
                               ("apply", "--summary", "x.patch"))

    def test_branch_keeps_refs(self):
        self.assert_refused(("branch", "-d", "x"), ("branch", "-D", "x"), ("branch", "--delete", "x"), ("branch", "-f", "x", "y"),
                            ("branch", "--force", "x"), ("branch", "-m", "x"), ("branch", "-M", "x"), ("branch", "-c", "x"), ("branch", "-C", "x"),
                            ("branch", "--edit-description"), ("branch", "-a", "x"), ("branch", "x", "y", "z"), ("branch", "-vD", "x"),
                            ("branch", "--list", "-D", "x"), ("branch", "--list", "-d", "x"))
        self.assert_reviewable(("branch",), ("branch", "--list"), ("branch", "-a"), ("branch", "-r"), ("branch", "-v"), ("branch", "-vv"),
                               ("branch", "--show-current"), ("branch", "--list", "sora/*"), ("branch", "fresh"), ("branch", "fresh", "HEAD~1"))

    def test_recovery_records_and_plumbing(self):
        self.assert_refused(("reflog", "expire", "--expire=now", "--all"), ("reflog", "delete", "HEAD@{0}"), ("reflog", "drop", "x"),
                            ("gc", "--prune=now"), ("gc",), ("notes", "prune"), ("read-tree", "-u", "--reset", "HEAD"), ("read-tree", "HEAD"),
                            ("update-index", "--force-remove", "a"), ("update-ref", "refs/heads/x", "HEAD~1"), ("symbolic-ref", "HEAD", "refs/heads/x"))
        self.assert_reviewable(("reflog",), ("reflog", "show"), ("reflog", "-5"), ("reflog", "show", "HEAD"))

    def test_output_is_still_refused_for_these(self):
        self.assert_refused(("stash", "show", "--output=x"), ("branch", "--output=x"), ("clean", "-n", "--output=x"),
                            ("reflog", "show", "--output=x"), ("switch", "--output=x"), ("commit", "-m", "x", "--", "--output=x"))

    def test_the_measured_safe_forms_stay_reviewable(self):
        self.assert_reviewable(("merge", "other"), ("pull", ".", "other"), ("merge", "--abort"), ("rebase", "--abort"),
                               ("cherry-pick", "--abort"), ("revert", "--abort"), ("add", "--", "a.txt"))

    def test_r6_forms_stay_reviewable(self):
        self.assert_reviewable(("status", "--short", "--branch", "--untracked-files=no"), ("add", "--", "docs/x.md"),
                               ("commit", "-m", "docs: x", "--", "docs/x.md"), ("show", "--stat", "--format=%H", "HEAD"),
                               ("push", "origin", WORK), ("ls-remote", "origin", "refs/heads/main"))

    def test_shell_forms(self):
        for argv in (("/c", "git reset --hard"), ("/c", "git", "clean", "-fdx"), ("/c", "git checkout -- a"), ("-c", "git restore ."),
                     ("/c", "git stash drop"), ("/c", "git rm -f a"), ("/c", "git mv -f a b"), ("-Command", "& git.exe RESET --hard")):
            with self.subTest(argv=argv):
                self.assertTrue(push_refused(r"C:\Windows\System32\cmd.exe", argv))
        for argv in (("/c", "dir"), ("/c", "git status"), ("-c", "echo reset")):
            with self.subTest(argv=argv):
                self.assertFalse(push_refused(r"C:\Windows\System32\cmd.exe", argv))


class KnownLimitTests(unittest.TestCase):
    """Documented gaps. These pin what the rule cannot see, so nobody assumes more."""

    def test_a_program_or_script_is_not_readable(self):
        self.assertFalse(push_refused(sys.executable, ("-c", "import subprocess; subprocess.run(['git','push'])")))
        self.assertFalse(push_refused(r"D:\x\deploy.exe", ("push", "origin", "main")))

    def test_where_origin_points_is_not_checked(self):
        # The rule reads text only. A local remote or URL change made earlier stays invisible.
        self.assertFalse(push_refused(GIT, ("push", "origin", WORK)))

    def test_the_github_cli_is_not_covered(self):
        self.assertFalse(push_refused(r"C:\x\gh.exe", ("api", "-X", "PATCH", "repos/x/y/git/refs/heads/main")))

    def test_configured_hooks_and_a_named_merge_strategy_are_not_checked(self):
        # A listed command still runs hooks and filters already configured, and -s finds git-merge-<name> on PATH.
        self.assertFalse(push_refused(GIT, ("commit", "-m", "x", "--", "a.txt")))
        self.assertFalse(push_refused(GIT, ("merge", "-s", "x", "y")))

    def test_a_program_that_runs_git_is_not_readable(self):
        self.assertFalse(push_refused(sys.executable, ("-c", "import subprocess; subprocess.run(['git','reset','--hard'])")))

    def test_a_folder_named_like_a_file_is_not_detected(self):
        # Text alone cannot tell. The window has to catch this one.
        self.assertFalse(push_refused(GIT, ("commit", "-m", "x", "--", ".github")))
        self.assertFalse(push_refused(GIT, ("add", "--", "d.old")))

    def test_tagging_is_not_limited(self):
        self.assertFalse(push_refused(GIT, ("tag", "-d", "x")))


if __name__ == "__main__":
    unittest.main()
