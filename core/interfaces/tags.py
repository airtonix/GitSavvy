from itertools import chain
import os
import re

import sublime
from sublime_plugin import WindowCommand

from ..commands import GsNavigate
from ...common import ui
from ..git_command import GitCommand, GitSavvyError
from ...common import util
from GitSavvy.core.fns import filter_
from GitSavvy.core.runtime import enqueue_on_worker, on_worker
from GitSavvy.core.utils import flash, uprint


__all__ = (
    "gs_show_tags",
    "gs_tags_toggle_remotes",
    "gs_tags_refresh",
    "gs_tags_delete",
    "gs_tags_push",
    "gs_tags_show_commit",
    "gs_tags_show_graph",
    "gs_tags_navigate_tag",
)


MYPY = False
if MYPY:
    from typing import List


TAG_DELETE_MESSAGE = "Tag(s) deleted."

NO_REMOTES_MESSAGE = "You have not configured any remotes."

NO_LOCAL_TAGS_MESSAGE = "    Your repository has no tags."
NO_REMOTE_TAGS_MESSAGE = "    The remote has no tags."
NO_MORE_TAGS_MESSAGE = "    No further tags on the remote."
REMOTE_ERRED = "    Unable to retrieve tags for this remote."
LOADING_TAGS_MESSAGE = "    Loading tags from remote..."

START_PUSH_MESSAGE = "Pushing tag..."
END_PUSH_MESSAGE = "Push complete."


class gs_show_tags(WindowCommand, GitCommand):

    """
    Open a branch dashboard for the active Git repository.
    """

    def run(self):
        ui.show_interface(self.window, self.repo_path, "tags")


class TagsInterface(ui.Interface, GitCommand):

    """
    Tags dashboard.
    """

    interface_type = "tags"
    syntax_file = "Packages/GitSavvy/syntax/tags.sublime-syntax"

    show_remotes = None
    remotes = None

    template = """\

      BRANCH:  {branch_status}
      ROOT:    {repo_root}
      HEAD:    {head}

      LOCAL:
    {local_tags}{remote_tags}
    {< help}
    """
    template_help = """
      #############                   ###########
      ## ACTIONS ##                   ## OTHER ##
      #############                   ###########

      [c] create                      [r]         refresh dashboard
      [s] create smart tag            [?]         toggle this help menu
      [d] delete                      [e]         toggle display of remote branches
      [p] push to remote              [tab]       transition to next dashboard
      [P] push all tags to remote     [SHIFT-tab] transition to previous dashboard
      [o] show commit
      [g] show log graph

    -
    """

    template_remote = """
      REMOTE ({remote_name}):
    {remote_tags_list}"""

    def title(self):
        return "TAGS: {}".format(os.path.basename(self.repo_path))

    def pre_render(self):
        if self.show_remotes is None:
            self.show_remotes = self.savvy_settings.get("show_remotes_in_tags_dashboard")

        self.max_items = self.savvy_settings.get("max_items_in_tags_dashboard", None)
        self.local_tags = self.get_local_tags()
        if self.remotes is None:
            self.remotes = {
                name: {"uri": uri}
                for name, uri in self.get_remotes().items()
            }

    def on_new_dashboard(self):
        self.view.run_command("gs_tags_navigate_tag")

    @ui.section("branch_status")
    def render_branch_status(self):
        return self.get_working_dir_status().long_status

    @ui.section("repo_root")
    def render_repo_root(self):
        return self.short_repo_path

    @ui.section("head")
    def render_head(self):
        return self.get_latest_commit_msg_for_head()

    @ui.section("local_tags")
    def render_local_tags(self):
        if not any(chain(*self.local_tags)):
            return NO_LOCAL_TAGS_MESSAGE

        regular_tags, versions = self.local_tags
        return "\n{}\n".format(" " * 60).join(  # need some spaces on the separator line otherwise
                                                # the syntax expects the remote section begins
            filter_((
                "\n".join(
                    "    {} {}".format(
                        self.get_short_hash(tag.sha),
                        tag.tag,
                    )
                    for tag in regular_tags[:self.max_items]
                ),
                "\n".join(
                    "    {} {:<10} {}{}".format(
                        self.get_short_hash(tag.sha),
                        tag.tag,
                        tag.human_date,
                        " ({})".format(tag.relative_date) if tag.relative_date != tag.human_date else ""
                    )
                    for tag in versions[:self.max_items]
                )
            ))
        )

    @ui.section("remote_tags")
    def render_remote_tags(self):
        if not self.remotes:
            return "\n"

        if not self.show_remotes:
            return self.render_remote_tags_off()

        output_tmpl = "\n"
        render_fns = []

        for remote_name, remote in self.remotes.items():
            tmpl_key = "remote_tags_list_" + remote_name
            output_tmpl += "{" + tmpl_key + "}\n"

            @ui.section(tmpl_key)
            def render_remote(remote=remote, remote_name=remote_name):
                return self.get_remote_tags_list(remote, remote_name)

            render_fns.append(render_remote)

        return output_tmpl, render_fns

    @ui.section("help")
    def render_help(self):
        help_hidden = self.view.settings().get("git_savvy.help_hidden")
        if help_hidden:
            return ""
        else:
            return self.template_help

    def get_remote_tags_list(self, remote, remote_name):
        if "tags" in remote:
            if remote["tags"]:
                seen = {tag.sha: tag.tag for tag in chain(*self.local_tags)}
                tags_list = [
                    tag
                    for tag in remote["tags"]
                    if tag.tag[-3:] != "^{}" and tag.sha not in seen
                ]
                msg = "\n".join(
                    "    {} {}".format(self.get_short_hash(tag.sha), tag.tag)
                    for tag in tags_list[:self.max_items]
                ) or NO_MORE_TAGS_MESSAGE

            else:
                msg = NO_REMOTE_TAGS_MESSAGE

        elif "erred" in remote:
            msg = remote["erred"]

        elif "loading" in remote:
            msg = LOADING_TAGS_MESSAGE

        else:
            def do_tags_fetch(remote=remote, remote_name=remote_name):
                try:
                    remote["tags"] = list(chain(*self.get_remote_tags(remote_name)))
                except GitSavvyError as e:
                    remote["erred"] = "    {}".format(e.stderr)
                self.render()

            enqueue_on_worker(do_tags_fetch)
            remote["loading"] = True
            msg = LOADING_TAGS_MESSAGE

        return self.template_remote.format(
            remote_name=remote_name,
            remote_tags_list=msg
        )

    def render_remote_tags_off(self):
        return "\n\n  ** Press [e] to toggle display of remote branches. **\n"


ui.register_listeners(TagsInterface)

TAGS_SELECTOR = "meta.git-savvy.tag.name"
SHA_SELECTOR = "constant.other.git-savvy.tags.sha1"


class TagsInterfaceCommand(ui.InterfaceCommand):
    interface_type = TagsInterface
    interface = None  # type: TagsInterface

    def selected_local_tags(self):
        # type: () -> List[str]
        return ui.extract_by_selector(
            self.view, TAGS_SELECTOR, self.region_name_for("local_tags"))

    def selected_local_commits(self):
        # type: () -> List[str]
        return ui.extract_by_selector(
            self.view, SHA_SELECTOR, self.region_name_for("local_tags"))

    def selected_remote_tags(self, remote_name):
        # type: (str) -> List[str]
        return ui.extract_by_selector(
            self.view, TAGS_SELECTOR, self.remote_section_name_for(remote_name))

    def selected_remote_commits(self, remote_name):
        # type: (str) -> List[str]
        return ui.extract_by_selector(
            self.view, SHA_SELECTOR, self.remote_section_name_for(remote_name))

    def remote_section_name_for(self, remote_name):
        # type: (str) -> str
        return self.region_name_for("remote_tags_list_" + remote_name)


class gs_tags_toggle_remotes(TagsInterfaceCommand):

    """
    Toggle display of the remote tags.
    """

    def run(self, edit, show=None):
        interface = self.interface
        interface.remotes = None
        if show is None:
            interface.show_remotes = not interface.show_remotes
        else:
            interface.show_remotes = show
        interface.render()


class gs_tags_refresh(TagsInterfaceCommand):

    """
    Refresh the tags dashboard.
    """

    def run(self, edit, reset_remotes=False):
        interface = self.interface
        if reset_remotes:
            interface.remotes = None

        util.view.refresh_gitsavvy(self.view)


DELETE_UNDO_MESSAGE = """\
GitSavvy: Deleted tag ({0}), in case you want to undo, run:
  $ git tag {0} {1}
"""
EXTRACT_COMMIT = re.compile(r"\(was (.+)\)")


class gs_tags_delete(TagsInterfaceCommand):

    """
    Delete selected tag(s).
    """

    @on_worker
    def run(self, edit):
        interface = self.interface
        self.delete_local(interface)
        self.delete_remote(interface)
        util.view.refresh_gitsavvy(self.view)

    def delete_local(self, interface):
        tags_to_delete = self.selected_local_tags()
        if not tags_to_delete:
            return

        for tag in tags_to_delete:
            rv = self.git("tag", "-d", tag)
            match = EXTRACT_COMMIT.search(rv.strip())
            if match:
                commit = match.group(1)
                uprint(DELETE_UNDO_MESSAGE.format(tag, commit))

        flash(self.view, TAG_DELETE_MESSAGE)
        util.view.refresh_gitsavvy(self.view)

    def delete_remote(self, interface):
        if not interface.remotes:
            return

        for remote_name, remote in interface.remotes.items():
            tags_to_delete = self.selected_remote_tags(remote_name)

            if tags_to_delete:
                self.git(
                    "push",
                    remote_name,
                    "--delete",
                    *("refs/tags/" + tag for tag in tags_to_delete)
                )

        flash(self.view, TAG_DELETE_MESSAGE)
        interface.remotes = None
        util.view.refresh_gitsavvy(self.view)


class gs_tags_push(TagsInterfaceCommand):

    """
    Displays a panel of all remotes defined for the repository, then push
    selected or all tag(s) to the selected remote.
    """

    @on_worker
    def run(self, edit, push_all=False):
        self.remotes = list(self.get_remotes().keys())
        if not self.remotes:
            self.window.show_quick_panel([NO_REMOTES_MESSAGE], None)
            return

        self.window.show_quick_panel(
            self.remotes,
            lambda idx: self.push_async(idx, push_all=push_all),
            flags=sublime.MONOSPACE_FONT
        )

    def push_async(self, remote_idx, push_all=False):
        if push_all:
            enqueue_on_worker(self.push_all, remote_idx)
        else:
            enqueue_on_worker(self.push_selected, remote_idx)

    def push_selected(self, remote_idx):
        # The user pressed `esc` or otherwise cancelled.
        if remote_idx == -1:
            return
        remote = self.remotes[remote_idx]

        tags_to_push = self.selected_local_tags()

        flash(self.view, START_PUSH_MESSAGE)
        self.git("push", remote, *("refs/tags/" + tag for tag in tags_to_push))
        flash(self.view, END_PUSH_MESSAGE)

        interface = self.interface
        interface.remotes = None
        util.view.refresh_gitsavvy(self.view)

    def push_all(self, remote_idx):
        # The user pressed `esc` or otherwise cancelled.
        if remote_idx == -1:
            return
        remote = self.remotes[remote_idx]
        flash(self.view, START_PUSH_MESSAGE)
        self.git("push", remote, "--tags")
        flash(self.view, END_PUSH_MESSAGE)

        interface = self.interface
        interface.remotes = None
        util.view.refresh_gitsavvy(self.view)


class gs_tags_show_commit(TagsInterfaceCommand):

    """
    Display the commit for the selected tag's hash.
    """

    @on_worker
    def run(self, edit):
        interface = self.interface
        commit_hashes = self.selected_local_commits()

        if interface.remotes:
            for remote_name in interface.remotes:
                commit_hashes += self.selected_remote_commits(remote_name)

        for commit_hash in commit_hashes:
            self.window.run_command("gs_show_commit", {"commit_hash": commit_hash})


class gs_tags_show_graph(TagsInterfaceCommand):
    def run(self, edit) -> None:
        # NOTE: We take the commit sha's here (instead of the tag names)
        #       because in the graph a tag ref takes the form e.g.
        #       `tag: 2.14.5`.  T.i we avoid prepending "tag: " here.
        commits = self.selected_local_commits()
        if not commits:
            return
        if len(commits) > 1:
            flash(self.view, "Can only follow one tag. Taking the first one")

        self.window.run_command('gs_graph', {
            'all': True,
            'follow': commits[0]
        })


class gs_tags_navigate_tag(GsNavigate):

    """
    Move cursor to the next (or previous) selectable file in the dashboard.
    """

    def get_available_regions(self):
        return [file_region
                for region in self.view.find_by_selector("meta.git-savvy.tag.name")
                for file_region in self.view.lines(region)]
