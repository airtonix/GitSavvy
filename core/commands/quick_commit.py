import sublime
from sublime_plugin import WindowCommand

from ..git_command import GitCommand
from ...common import util
from ..ui_mixins.input_panel import show_single_line_input_panel


COMMIT_MSG_PROMPT = "Commit message:"


class GsQuickCommitCommand(WindowCommand, GitCommand):

    """
    Present the user with a input panel where they can enter a commit message.
    Once provided, perform a commit with that message.
    """

    def run(self):
        show_single_line_input_panel(
            COMMIT_MSG_PROMPT,
            "",
            lambda msg: sublime.set_timeout_async(lambda: self.on_done(msg), 0)
        )

    def on_done(self, commit_message):
        self.view.window().status_message("Commiting...")
        self.git("commit", "-q", "-F", "-", stdin=commit_message)
        self.window.status_message("Committed successfully.")
        util.view.refresh_gitsavvy(self.window.active_view())


class GsQuickStageCurrentFileCommitCommand(WindowCommand, GitCommand):

    """
    Present the user with a input panel where they can enter a commit message.
    Once provided, stage the current file and perform a commit with the
    provided message.
    """

    def run(self):
        show_single_line_input_panel(
            COMMIT_MSG_PROMPT,
            "",
            lambda msg: sublime.set_timeout_async(lambda: self.on_done(msg), 0)
        )

    def on_done(self, commit_message):
        self.view.window().status_message("Commiting...")
        self.git("add", "--", self.file_path)
        self.git("commit", "-q", "-F", "-", stdin=commit_message)
        self.window.status_message("Committed successfully.")
        util.view.refresh_gitsavvy(self.window.active_view())
