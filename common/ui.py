from collections import OrderedDict
from textwrap import dedent
import re

import sublime
from sublime_plugin import TextCommand

from . import util
from ..core.runtime import enqueue_on_worker
from ..core.settings import GitSavvySettings
from ..core.utils import focus_view
from GitSavvy.core.base_commands import GsTextCommand
from GitSavvy.core.fns import flatten
from GitSavvy.core.view import replace_view_content


__all__ = (
    "gs_new_content_and_regions",
    "gs_update_region",
    "gs_interface_close",
    "gs_interface_refresh",
    "gs_interface_toggle_help",
    "gs_interface_toggle_popup_help",
    "gs_edit_view_complete",
    "gs_edit_view_close",
)


MYPY = False
if MYPY:
    from typing import Dict, Iterable, Iterator, List, Optional, Protocol, Set, Tuple, Type, Union
    SectionRegions = Dict[str, sublime.Region]

    class SectionFn(Protocol):
        key = ''  # type: str

        def __call__(self) -> 'Union[str, Tuple[str, List[SectionFn]]]':
            pass


interfaces = {}  # type: Dict[sublime.ViewId, Interface]
edit_views = {}
subclasses = []

EDIT_DEFAULT_HELP_TEXT = "## To finalize your edit, press {super_key}+Enter.  To cancel, close the view.\n"


class _PrepareInterface(type):
    def __init__(cls, cls_name, bases, attrs):
        for attr_name, value in attrs.items():
            if attr_name.startswith("template"):
                setattr(cls, attr_name, dedent(value))


class Interface(metaclass=_PrepareInterface):
    interface_type = ""
    syntax_file = ""
    template = ""
    partials = {}  # type: Dict[str, SectionFn]

    _initialized = False

    def __new__(cls, repo_path=None, **kwargs):
        """
        Search for intended interface in active window - if found, bring it
        to focus and return it instead of creating a new interface.
        """
        if repo_path is not None:
            window = sublime.active_window()
            for view in window.views():
                vset = view.settings()
                if (
                    vset.get("git_savvy.interface") == cls.interface_type
                    and vset.get("git_savvy.repo_path") == repo_path
                ):
                    focus_view(view)
                    try:
                        return interfaces[view.id()]
                    except KeyError:
                        return cls(view=view)  # surprise! we recurse

        return super().__new__(cls)

    def __init__(self, repo_path=None, view=None):
        if self._initialized:
            return
        self._initialized = True

        subclass_attrs = (getattr(self, attr) for attr in vars(self.__class__).keys())

        self.partials = {
            attr.key: attr
            for attr in subclass_attrs
            if callable(attr) and hasattr(attr, "key")
        }

        if view:
            self.view = view
        else:
            self.create_view(repo_path)
            sublime.set_timeout_async(self.on_new_dashboard, 0)

        interfaces[self.view.id()] = self
        self.on_create()

    def create_view(self, repo_path):
        window = sublime.active_window()
        self.view = window.new_file()

        self.view.settings().set("git_savvy.repo_path", repo_path)
        self.view.settings().set("git_savvy.{}_view".format(self.interface_type), True)
        self.view.settings().set("git_savvy.tabbable", True)
        self.view.settings().set("git_savvy.interface", self.interface_type)
        self.view.settings().set("git_savvy.help_hidden", GitSavvySettings().get("hide_help_menu"))
        self.view.set_syntax_file(self.syntax_file)
        self.view.set_scratch(True)
        self.view.set_read_only(True)
        util.view.disable_other_plugins(self.view)
        self.after_view_creation(self.view)

        # Set title as late as possible, otherwise e.g. `result_file_regex` will not apply
        # after the initial activate. (It applies after the second activation of the view,
        # shall we say a sublime nuance.)
        self.view.set_name(self.title())

        self.render()
        focus_view(self.view)

        return self.view

    def title(self):
        # type: () -> str
        raise NotImplementedError

    def after_view_creation(self, view):
        """
        Hook template method called after the view has been created.
        Can be used to further manipulate the view and store state on it.
        """
        pass

    def on_new_dashboard(self):
        """
        Hook template method called after the first render.
        """
        pass

    def on_create(self):
        """
        Hook template method called after a new interface object has been created.
        """
        pass

    def on_close(self):
        """
        Hook template method called after a view has been closed.
        """
        pass

    def pre_render(self):
        pass

    def reset_cursor(self):
        pass

    def render(self, nuke_cursors=False):
        self.pre_render()
        content, regions = self._render_template()
        self.draw(content, regions)
        if nuke_cursors:
            self.reset_cursor()

    def draw(self, content, regions):
        # type: (str, SectionRegions) -> None
        self.view.run_command("gs_new_content_and_regions", {
            "content": content,
            "regions": {key: region_as_tuple(region) for key, region in regions.items()}
        })

    def _render_template(self):
        # type: () -> Tuple[str, SectionRegions]
        """
        Generate new content for the view given the interface template
        and partial content.  As partial content is added to the rendered
        template, compute and build up `regions` with the key, start, and
        end of each partial.
        """
        rendered = self.template
        regions = {}  # type: SectionRegions

        keyed_content = self.get_keyed_content()
        for key, new_content in keyed_content.items():
            new_content_len = len(new_content)
            pattern = re.compile(r"\{(<+ )?" + key + r"\}")

            match = pattern.search(rendered)
            while match:
                start, end = match.span()
                backspace_group = match.groups()[0]
                backspaces = backspace_group.count("<") if backspace_group else 0
                start -= backspaces

                rendered = rendered[:start] + new_content + rendered[end:]

                self._adjust_region_positions(regions, start, end - start, new_content_len)
                if new_content_len:
                    regions[key] = sublime.Region(start, start + new_content_len)

                match = pattern.search(rendered)

        return rendered, regions

    def _adjust_region_positions(self, regions, idx, orig_len, new_len):
        # type: (SectionRegions, int, int, int) -> None
        """
        When interpolating template variables, update region ranges for previously-evaluated
        variables, that are situated later on in the output/template string.
        """
        shift = new_len - orig_len
        for key, region in regions.items():
            if region.a > idx:
                region.a += shift
                region.b += shift
            elif region.b > idx or region.a == idx:
                region.b += shift

    def get_keyed_content(self):
        keyed_content = OrderedDict(
            (key, render_fn())
            for key, render_fn in self.partials.items()
        )

        for key in keyed_content:
            output = keyed_content[key]
            if isinstance(output, tuple):
                sub_template, complex_partials = output
                keyed_content[key] = sub_template

                for render_fn in complex_partials:
                    keyed_content[render_fn.key] = render_fn()

        return keyed_content

    def update_view_section(self, key, content):
        self.view.run_command("gs_update_region", {
            "key": "git_savvy_interface." + key,
            "content": content
        })


def section(key):
    def decorator(fn):
        fn.key = key
        return fn
    return decorator


class gs_new_content_and_regions(TextCommand):
    current_region_names = set()  # type: Set[str]

    def run(self, edit, content, regions):
        replace_view_content(self.view, content)

        for key, region_range in regions.items():
            self.view.add_regions("git_savvy_interface." + key, [region_from_tuple(region_range)])

        for key in self.current_region_names - regions.keys():
            self.view.erase_regions("git_savvy_interface." + key)

        self.current_region_names = regions.keys()

        if self.view.settings().get("git_savvy.interface"):
            self.view.run_command("gs_handle_vintageous")
            self.view.run_command("gs_handle_arrow_keys")


class gs_update_region(TextCommand):

    def run(self, edit, key, content):
        is_read_only = self.view.is_read_only()
        self.view.set_read_only(False)
        for region in self.view.get_regions(key):
            self.view.replace(edit, region, content)
        self.view.set_read_only(is_read_only)


def register_listeners(InterfaceClass):
    subclasses.append(InterfaceClass)


def get_interface(view_id):
    # type: (sublime.ViewId) -> Optional[Interface]
    return interfaces.get(view_id, None)


class InterfaceCommand(GsTextCommand):
    interface_type = None  # type: Type[Interface]
    interface = None  # type: Interface

    def run_(self, edit_token, args):
        vid = self.view.id()
        interface = get_interface(vid)
        if not interface:
            raise RuntimeError(
                "Assertion failed! "
                "no dashboard registered for {}".format(vid))
        if not isinstance(interface, self.interface_type):
            raise RuntimeError(
                "Assertion failed! "
                "registered interface `{}` is not of type `{}`"
                .format(interface, self.interface_type.__name__)
            )
        self.interface = interface
        return super().run_(edit_token, args)

    def region_name_for(self, section):
        # type: (str) -> str
        return "git_savvy_interface." + section


def region_as_tuple(region):
    # type: (sublime.Region) -> Tuple[int, int]
    return region.begin(), region.end()


def region_from_tuple(tuple_):
    # type: (Tuple[int, int]) -> sublime.Region
    return sublime.Region(*tuple_)


def unique_regions(regions):
    # type: (Iterable[sublime.Region]) -> Iterator[sublime.Region]
    # Regions are not hashable so we unpack them to tuples,
    # then use set, finally pack them again
    return map(region_from_tuple, set(map(region_as_tuple, regions)))


def unique_selected_lines(view):
    # type: (sublime.View) -> List[sublime.Region]
    return list(unique_regions(flatten(view.lines(s) for s in view.sel())))


def extract_by_selector(view, item_selector, within_section=None):
    # type: (sublime.View, str, str) -> List[str]
    selected_lines = unique_selected_lines(view)
    items = view.find_by_selector(item_selector)
    acceptable_sections = (
        view.get_regions(within_section)
        if within_section else
        [sublime.Region(0, view.size())]
    )
    return [
        view.substr(item)
        for section in acceptable_sections
        for line in selected_lines if section.contains(line)
        for item in items if line.contains(item)
    ]


class gs_interface_close(TextCommand):

    """
    Clean up references to interfaces for closed views.
    """

    def run(self, edit):
        view_id = self.view.id()
        interface = get_interface(view_id)
        if interface:
            interface.on_close()
            enqueue_on_worker(lambda: interfaces.pop(view_id))


class gs_interface_refresh(TextCommand):

    """
    Re-render GitSavvy interface view.
    """

    def run(self, edit, nuke_cursors=False):
        enqueue_on_worker(self.run_async, nuke_cursors)

    def run_async(self, nuke_cursors):
        # type: (bool) -> None
        interface_type = self.view.settings().get("git_savvy.interface")
        for cls in subclasses:
            if cls.interface_type == interface_type:
                vid = self.view.id()
                interface = interfaces.get(vid, None)
                if not interface:
                    interface = interfaces[vid] = cls(view=self.view)
                interface.render(nuke_cursors=nuke_cursors)  # type: ignore[union-attr]
                break


class gs_interface_toggle_help(TextCommand):

    """
    Toggle GitSavvy help.
    """

    def run(self, edit):
        interface_type = self.view.settings().get("git_savvy.interface")
        for InterfaceSubclass in subclasses:
            if InterfaceSubclass.interface_type == interface_type:
                current_help = bool(self.view.settings().get("git_savvy.help_hidden"))
                self.view.settings().set("git_savvy.help_hidden", not current_help)
                self.view.run_command("gs_interface_refresh")


class gs_interface_toggle_popup_help(TextCommand):

    """
    Toggle GitSavvy popup help.
    """

    def run(self, edit, view_name, popup_max_width=800, popup_max_height=900):
        css = sublime.load_resource("Packages/GitSavvy/popups/style.css")
        html = (
            sublime.load_resource("Packages/GitSavvy/popups/" + view_name + ".html")
            .format(css=css, super_key=util.super_key)
        )
        visible_region = self.view.visible_region()
        self.view.show_popup(html, 0, visible_region.begin(), popup_max_width, popup_max_height)


class EditView():

    def __init__(self, content, on_done, repo_path, help_text=None, window=None):
        self.window = window or sublime.active_window()
        self.view = self.window.new_file()

        self.view.set_scratch(True)
        self.view.set_read_only(False)
        self.view.set_name("EDIT")
        self.view.set_syntax_file("Packages/GitSavvy/syntax/make_commit.sublime-syntax")
        self.view.settings().set("git_savvy.edit_view", True)
        self.view.settings().set("git_savvy.repo_path", repo_path)

        self.on_done = on_done
        self.render(content, help_text)

        edit_views[self.view.id()] = self

    def render(self, starting_content, help_text):
        regions = {}

        starting_content += "\n\n"

        regions["content"] = (0, len(starting_content))
        content = starting_content + (help_text or EDIT_DEFAULT_HELP_TEXT).format(super_key=util.super_key)
        regions["help"] = (len(starting_content), len(content))

        self.view.run_command("gs_new_content_and_regions", {
            "content": content,
            "regions": regions
        })


class gs_edit_view_complete(TextCommand):

    """
    Invoke callback with edit view content.
    """

    def run(self, edit):
        sublime.set_timeout_async(self.run_async, 0)

    def run_async(self):
        edit_view = edit_views.get(self.view.id(), None)
        if not edit_view:
            sublime.error_message("Unable to complete edit.  Please try again.")
            return

        help_region = self.view.get_regions("git_savvy_interface.help")[0]
        content_before = self.view.substr(sublime.Region(0, help_region.begin()))
        content_after = self.view.substr(sublime.Region(help_region.end(), self.view.size() - 1))
        content = (content_before + content_after).strip()

        self.view.close()
        edit_view.on_done(content)


class gs_edit_view_close(TextCommand):

    """
    Clean up references to closed edit views.
    """

    def run(self, edit):
        sublime.set_timeout_async(self.run_async, 0)

    def run_async(self):
        view_id = self.view.id()
        if view_id in edit_views:
            del edit_views[view_id]
