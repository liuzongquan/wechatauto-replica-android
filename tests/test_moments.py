"""Synthetic feed observations and scoped menu actions, never native SNS IDs."""
from dataclasses import replace
from xml.sax.saxutils import quoteattr

import pytest

from fakes import FakeAdapter, fixture_profile, PACKAGE
from wechatauto_android import SafetyPolicy, WeChat
from wechatauto_android.errors import (AmbiguousTarget, IdentityMismatch, PermissionDenied,
    SelectorNotFound, StaleSnapshot, ValidationError, OutcomeUnknown, DuplicateOperation)
from wechatauto_android.moments import MOMENT_CONTEXT_ROLES
from wechatauto_android.ui import Hierarchy


class MomentFake(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.posts = [["Display author", "A visible post", "12:34"], ["Other author", "Other post", "11:00"]]
        self.comment = ""
        self.selected_post = None
        self.liked = False
        self.menu_false = False

    def _items(self):
        if self.state == "discover":
            return [("discover.moments", "朋友圈")]
        if self.state in {"feed", "moment_menu", "moment_comment"}:
            items = [("moments.feed", "朋友圈")]
            if self.state == "moment_menu":
                items += [("moments.unlike" if self.liked else "moments.like", "取消" if self.liked else "赞"), ("moments.comment", "评论")]
            if self.state == "moment_comment":
                items += [("moments.comment_input", self.comment), ("moments.comment_send", "发送")]
            return items
        items = super()._items()
        if self.state == "home":
            items += [("home.discover", "发现")]
        return items

    @staticmethod
    def node(role, text, y, children=""):
        attrs = {"text": text, "resource-id": PACKAGE + ":id/synthetic_" + role,
                 "package": PACKAGE, "enabled": "true", "clickable": "true",
                 "bounds": f"[0,{y}][200,{y + 10}]"}
        return "<node " + " ".join(f"{key}={quoteattr(value)}" for key, value in attrs.items()) + ">" + children + "</node>"

    def hierarchy(self):
        if self.state not in {"feed", "moment_menu", "moment_comment"}:
            return super().hierarchy()
        elements = [self.node(role, text, index * 10) for index, (role, text) in enumerate(self._items())]
        for index, (author, content, timestamp) in enumerate(self.posts):
            y = 100 + index * 100
            children = self.node("moments.author", author, y)
            children += self.node("moments.content", content, y + 10)
            children += self.node("moments.timestamp", timestamp, y + 20)
            children += self.node("moments.menu", "more", y + 30)
            elements.append(self.node("moments.rows", "", y, children))
        return Hierarchy.parse("<hierarchy>" + "".join(elements) + "</hierarchy>")

    def click(self, selector, *, expected_fingerprint=None):
        role = self._role(selector, expected_fingerprint)
        if role == "home.discover":
            self.state = "discover"
        elif role == "discover.moments":
            self.state = "feed"
        elif role == "moments.comment":
            self.state = "moment_comment"
        elif role in {"moments.like", "moments.unlike", "moments.comment_send"}:
            self.commits.append(role)
            if self.error_commit:
                raise TimeoutError("uncertain synthetic commit")
            if role != "moments.comment_send":
                self.liked = not self.liked
            self.state = "feed"
        else:
            return super().click(selector, expected_fingerprint=expected_fingerprint)

    def click_node(self, node, *, expected_fingerprint):
        assert self.hierarchy().fingerprint == expected_fingerprint
        self.selected_post = (node.bounds[1] - 130) // 100
        self.state = "moment_menu"
        if self.menu_false:
            return False

    def set_text(self, selector, text):
        role = self._role(selector)
        if role == "moments.comment_input":
            self.comment = text
        else:
            return super().set_text(selector, text)

    def press_back(self):
        if self.state in {"feed", "moment_menu", "moment_comment"}:
            self.state = "home"
        else:
            super().press_back()


@pytest.fixture
def setup_moments(tmp_path):
    fake = MomentFake()
    profile = fixture_profile()
    roles = dict(profile.roles)
    for role in MOMENT_CONTEXT_ROLES:
        roles[role] = {"resource_id": PACKAGE + ":id/synthetic_" + role}
    profile = replace(profile, roles=roles, collections={"moments_rows": {"resource_id": PACKAGE + ":id/synthetic_moments.rows"}})
    policy = SafetyPolicy("SERIAL", "wxid_self", allowed_actions=frozenset({"moment_like", "moment_unlike", "moment_comment"}), execute=True, assume_exclusive_device=True)
    client = WeChat(fake, profile, policy, tmp_path / "journal.sqlite", transition_timeout=.001)
    return fake, client


def authorize(client, reference):
    client.policy = replace(client.policy, allowed_targets=frozenset({reference.id}))


def test_observations_are_snapshot_scoped_not_native_identity(setup_moments):
    _, client = setup_moments
    rows = client.Moment.observe()
    assert len(rows) == 2
    assert rows[0].id.startswith("moment-observation:")
    assert rows[0].author_display == "Display author"
    assert rows[0].origin == "android_visible_moment_observation"
    assert "wxid" not in rows[0].author_display
    assert rows[0].snapshot_fingerprint and rows[0].row_signature
    assert rows[0].to_dict()["assurance"].startswith("prior_account")

@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("action", ["like", "unlike", "comment"])
def test_post_scoped_menu_and_commit(setup_moments, index, action):
    fake, client = setup_moments
    reference = client.Moment.observe()[index]
    authorize(client, reference)
    if action == "unlike":
        fake.liked = True
    if action == "comment":
        result = client.Moment.comment(reference, "中文评论😀", operation_id="moment-comment")
        assert fake.comment == "中文评论😀"
    else:
        result = client.Moment.like(reference, operation_id="moment-like", unlike=action == "unlike")
    assert result.status == "submitted" and not result.success
    assert fake.selected_post == index
    assert len(fake.commits) == 1


def test_per_reference_allowlist_not_author_display(setup_moments):
    fake, client = setup_moments
    ref = client.Moment.observe()[0]
    client.policy = replace(client.policy, allowed_targets=frozenset({ref.author_display}))
    with pytest.raises(PermissionDenied):
        client.Moment.like(ref, operation_id="not-allowed-post")
    assert not fake.commits


def test_stale_feed_refused_before_menu(setup_moments):
    fake, client = setup_moments
    ref = client.Moment.observe()[0]
    authorize(client, ref)
    fake.posts[0][1] = "Changed post"
    with pytest.raises(StaleSnapshot):
        client.Moment.like(ref, operation_id="stale-post-ref")
    assert fake.selected_post is None and not fake.commits


def test_duplicate_observed_post_refused(setup_moments):
    fake, client = setup_moments
    fake.posts[1] = list(fake.posts[0])
    with pytest.raises(AmbiguousTarget):
        client.Moment.observe()

@pytest.mark.parametrize("field,value", [("account_id", "other"), ("device_serial", "other"), ("profile_name", "other"), ("wechat_version", "8.0.1")])
def test_reference_namespace_isolation(setup_moments, field, value):
    fake, client = setup_moments
    ref = client.Moment.observe()[0]
    with pytest.raises(IdentityMismatch):
        client.Moment.like(replace(ref, **{field: value}), operation_id="wrong-reference")
    assert not fake.commits


def test_modified_reference_fields_refused(setup_moments):
    _, client = setup_moments
    ref = client.Moment.observe()[0]
    with pytest.raises(ValidationError):
        client.Moment.like(replace(ref, content_visible="tampered"), operation_id="tampered-reference")


def test_like_never_silently_unlikes(setup_moments):
    fake, client = setup_moments
    ref = client.Moment.observe()[0]
    authorize(client, ref)
    fake.liked = True
    with pytest.raises(SelectorNotFound):
        client.Moment.like(ref, operation_id="already-liked-post")
    assert not fake.commits


def test_uncertain_commit_cannot_replay(setup_moments):
    fake, client = setup_moments
    ref = client.Moment.observe()[0]
    authorize(client, ref)
    fake.error_commit = True
    with pytest.raises(OutcomeUnknown):
        client.Moment.like(ref, operation_id="uncertain-moment")
    with pytest.raises(DuplicateOperation):
        client.Moment.like(ref, operation_id="uncertain-moment")
    assert len(fake.commits) == 1


def test_read_observation_requires_account_assurance(setup_moments):
    _, client = setup_moments
    client.policy = replace(client.policy, assume_exclusive_device=False)
    with pytest.raises(PermissionDenied):
        client.Moment.observe()


def test_noop_dryrun_does_not_navigate(setup_moments):
    fake, client = setup_moments
    ref = client.Moment.observe()[0]
    client.policy = replace(client.policy, execute=False)
    before = fake.state
    result = client.Moment.comment(ref, "planned", operation_id="dry-moment-comment")
    assert result.status == "dry_run" and fake.state == before
    assert not fake.commits
