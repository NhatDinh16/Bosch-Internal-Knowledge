# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""Viva Engage GraphQL client (Yammer successor).

Every operation is one POST to a single GraphQL endpoint via
:class:`~jarvis.engage.session.EngageSession`. Most reads send a self-healing ad-hoc
query document from ``QUERIES``; a minority the server gates to persisted mode send a
sha256 hash from ``HASHES`` (``_gql`` prefers a document and falls back to a hash).
See ``references/api.md`` for the endpoint, token, and mutation body shapes.
"""

from __future__ import annotations

import uuid
from typing import Any

from .content import (
    build_draftjs_content,
    decode_id,
    encode_id,
    extract_plain_text,
    format_group,
    format_thread,
    to_node_id,
)
from .session import EngageSession

# ── Persisted-query hashes (server-gated operations only) ────────────────────
# A few operations return "Field temporarily unavailable" as ad-hoc query text, so
# they cannot move to QUERIES and must send the sha256 hash of the query document
# baked into the web client. A stale hash returns PersistedQueryNotFound; re-capture
# from live engage.cloud.microsoft/graphql traffic (web-capture skill; for mutations,
# perform a real post/reply/like). Everything else self-heals via QUERIES.
HASHES = {
    "FeedHomeNestedClients": "a2cc812a4ea446ec6e24d75d0316493fc11240890b02f7b9534910521beb8daa",
    "InboxFeedQuery": "c185e3f7db75da54468fd2534e7985e38e1223df6f455c421842664e5624d220",
    "PublishGroupMessageClients": (
        "cb075e34eaeb96d0539118bcea5a6fc4e3419dd9807c1d9dcdbb6cc89a8d2feb"
    ),
    "PublishUserMessageClients": "c0b32c50203bc86c825db9feac81ef00bf901b73dec032f24dde50aad939544c",
    "PublishReplyMessageClients": (
        "a8aab87623aeb5fb91968b0ba736754e24f57186261b1ad8d4b34020e92e8f82"
    ),
    "LikeClients": "1675f03021fc87d1abb14bfdbc9bca1dfdd3413e2e6658c7be9a78cf0a87fba8",
}

# ── Ad-hoc query documents (self-healing) ────────────────────────────────────
# The endpoint accepts full query text (not safelist-locked), so sending our own
# documents removes any dependence on the server-side persisted-query manifest - a
# client redeploy can no longer break these (no PersistedQueryNotFound). The field
# set is the minimum the formatters read. Home feed and inbox thread listing cannot
# be served ad-hoc (they return "Field temporarily unavailable") and stay on HASHES.
#
# Fidelity trade-offs of the ad-hoc fields: the group feed, notifications, and
# group-members fields do not expose client-side count/sort/cursor/role arguments,
# so server defaults apply (group feed page size and sort, members page of 50, the
# role filter is not applied server-side). Notification count is enforced
# client-side; the CLI surfaces the rest as explicit warnings.

_MSG = (
    "id createdAt isEdited isQuestion isArticle "
    "sender { __typename ... on User { displayName email } } "
    "body { serializedContentState } "
    "reactionsConnection { totalCount }"
)
_THREAD = (
    "id databaseId createdAt updatedAt seenByCount "
    "isAnnouncement isClosed isDirectMessage viewerHasUnreadMessages "
    "group { id displayName } "
    "topics { edges { node { name: displayName } } } "
    "threadStarter { " + _MSG + " attachments { edges { node { __typename } } } }"
)
_GROUP = (
    "id databaseId displayName description privacy state isOfficial isExternal "
    "viewerMembershipStatus viewerIsAdmin viewerCanStartThread"
)

QUERIES = {
    "CurrentUserClients": (
        "query CurrentUserClients { viewer { locale isNetworkAdmin "
        "user { id databaseId displayName email jobTitle "
        "network { displayName permalink } } } }"
    ),
    "SearchClients": (
        "query SearchClients($searchText:String!,$count:Int!){ "
        "search(query:$searchText){ threads(first:$count){ edges { node { "
        + _THREAD
        + " } } pageInfo { hasNextPage endCursor } } } }"
    ),
    "SearchResultsGroupQuery": (
        "query SearchResultsGroupQuery($searchText:String!){ "
        "search(query:$searchText){ groups(first:20){ edges { node { " + _GROUP + " } } } } }"
    ),
    "NotificationListInitialQuery": (
        "query NotificationListInitialQuery { viewer { notifications { edges { node { "
        "id createdAt isUnseen details { __typename "
        "... on ReactionNotification { reactingUsersCount "
        "featuredReactions { reaction user { displayName } } "
        "thread { id group { displayName } threadStarter { " + _MSG + " } } } "
        "} } } } } }"
    ),
    "MyGroupsClients": (
        "query MyGroupsClients($groupsCount:Int!){ viewer { "
        "groups(first:$groupsCount){ edges { node { " + _GROUP + " } } } } }"
    ),
    "OfficialGroupsClients": (
        "query OfficialGroupsClients($groupsCount:Int!){ "
        "officialGroups(first:$groupsCount){ edges { node { " + _GROUP + " } } } }"
    ),
    "SuggestedGroupsClients": (
        "query SuggestedGroupsClients($groupsCount:Int!){ viewer { "
        "suggestedGroups(first:$groupsCount){ edges { node { " + _GROUP + " } } } } }"
    ),
    "NavigationGroupsClients": (
        "query NavigationGroupsClients { viewer { "
        "groups(first:50){ edges { node { id databaseId displayName } } } "
        "favoriteGroups { edges { node { id displayName } } } } }"
    ),
    "InboxUnreadCountClients": (
        "query InboxUnreadCountClients { viewer { inbox { "
        "inboxUnreadCount { unreadThreadsInfo { threadId } } } } }"
    ),
    "FeedGroupNestedClients": (
        "query FeedGroupNestedClients($databaseId:String!){ "
        "group(databaseId:$databaseId){ displayName feed { threads { edges { node { "
        + _THREAD
        + " } } pageInfo { hasNextPage } } } } }"
    ),
    "NestedThreadClients": (
        "query NestedThreadClients($threadId:ID!,$replyCount:Int!){ "
        "thread: node(id:$threadId){ __typename ... on Thread { " + _THREAD + " "
        "topLevelReplies(first:$replyCount){ edges { node { " + _MSG + " } } "
        "pageInfo { hasNextPage } } } } }"
    ),
    "GroupHeaderClients": (
        "query GroupHeaderClients($databaseId:String!){ "
        "group(databaseId:$databaseId){ " + _GROUP + " createdAt "
        "network { displayName } isDynamicMembership isAllCompanyGroup } }"
    ),
    "GroupRoleMembersClients": (
        "query GroupRoleMembersClients($databaseId:String!){ "
        "group(databaseId:$databaseId){ members(first:50){ edges { node { "
        "id displayName email jobTitle } } } } }"
    ),
}

# The API rejects any connection page above this ("'last' must be <= 20") on
# thread replies, the home feed, and the inbox; the home feed pages transparently.
MAX_PAGE = 20
MAX_REPLIES = MAX_PAGE


class EngageClient:
    """High-level Viva Engage GraphQL API client."""

    def __init__(self, session: EngageSession | None = None):
        self._session = session or EngageSession.from_session()

    def _gql(self, operation: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._session.graphql(
            operation, QUERIES.get(operation), HASHES.get(operation), variables
        )

    # ── Current user ────────────────────────────────────────────────────

    def get_current_user(self) -> dict[str, Any]:
        """The authenticated user's profile."""
        data = self._gql("CurrentUserClients")
        viewer = data.get("viewer", {})
        user = viewer.get("user", {})
        network = user.get("network", {})
        return {
            "userId": user.get("id"),
            "databaseId": user.get("databaseId"),
            "displayName": user.get("displayName"),
            "email": user.get("email"),
            "jobTitle": user.get("jobTitle"),
            "network": network.get("displayName"),
            "networkPermalink": network.get("permalink"),
            "locale": viewer.get("locale"),
            "isNetworkAdmin": viewer.get("isNetworkAdmin"),
        }

    # ── Home feed ───────────────────────────────────────────────────────

    def get_home_feed(self, count: int = 10, older_than: str | None = None) -> dict[str, Any]:
        """The home feed, transparently paginated to *count* threads.

        The server caps one page at 20 (``'last' must be <= 20``) and the only
        ``HomeFeedType`` value it accepts is ``DISCOVERY``; larger counts follow
        the ``endCursor`` -> ``olderThan`` chain across pages.
        """
        threads: list[dict[str, Any]] = []
        page_info: dict[str, Any] | None = None
        cursor = older_than
        while len(threads) < count:
            page_size = min(count - len(threads), MAX_PAGE)
            variables: dict[str, Any] = {
                "threadCount": page_size,
                "replyCount": 2,
                "sortRepliesBy": "UPVOTE_RANK_THEN_CREATED_AT",
                "requestContentInTargetLanguage": True,
                "contentTargetLanguage": "en-us",
                "feedType": "DISCOVERY",
                "skipRealtime": True,
                "includeViewerIsFollowingSender": False,
                "includeInFeedUserSuggestions": False,
                "includeHiddenForNetworkInDiscovery": True,
                "includeViewerHasBookmarked": True,
                "includeOriginNetworkBadge": True,
                "includeUserHideFields": True,
                "includeVerifiedReply": True,
                "includeRecommendedTopLevelReplies": True,
            }
            if cursor:
                variables["olderThan"] = cursor
            data = self._gql("FeedHomeNestedClients", variables)
            feed = data.get("viewer", {}).get("homeFeed", {}).get("homeFeedCards", {})
            edges = feed.get("edges", [])
            page_info = feed.get("pageInfo")
            threads.extend(format_thread(e["node"]) for e in edges if e.get("node"))
            # hasNextPage is unreliable on this feed; stop on a short page or no cursor.
            cursor = (page_info or {}).get("endCursor")
            if len(edges) < page_size or not cursor:
                break
        return {"threads": threads, "pageInfo": page_info}

    # ── Community feed ──────────────────────────────────────────────────

    def get_community_feed(
        self,
        group_id: str,
        count: int = 10,
        sort_by: str = "CREATED_AT",
        feed_type: str = "ALL",
        older_than: str | None = None,
    ) -> dict[str, Any]:
        """A community's feed. *group_id* is a node id or numeric database id.

        The ad-hoc field exposes no count/sort/cursor arguments, so *count*,
        *sort_by*, and *older_than* are sent but not applied - the server's
        default page and CREATED_AT order come back.
        """
        group_id = to_node_id("Group", group_id)
        variables: dict[str, Any] = {
            "threadCount": count,
            "replyCount": 2,
            "sortRepliesBy": "UPVOTE_RANK_THEN_CREATED_AT",
            "sortThreadsBy": sort_by,
            "requestContentInTargetLanguage": True,
            "contentTargetLanguage": "en-us",
            "groupId": group_id,
            "databaseId": decode_id(group_id)["id"],
            "groupFeedType": feed_type,
            "includeSenderBadges": True,
            "includeHiddenForNetworkInDiscovery": True,
            "includeViewerHasBookmarked": True,
            "includeOriginNetworkBadge": True,
            "includeUserHideFields": True,
            "includeGroupFeedLastVisitedAt": True,
            "includeGroupFeedLastVisitedIds": True,
            "includeVerifiedReply": True,
            "includeMessageContentSourceFile": True,
        }
        if older_than:
            variables["olderThan"] = older_than
        data = self._gql("FeedGroupNestedClients", variables)
        group_data = data.get("group")
        if not group_data:
            raise LookupError(f"community not found or not accessible: {group_id}")
        feed = group_data.get("feed", {}).get("threads", {})
        edges = feed.get("edges", [])
        return {
            "communityName": group_data.get("displayName"),
            "threads": [format_thread(e["node"]) for e in edges if e.get("node")],
            "pageInfo": feed.get("pageInfo"),
        }

    # ── Thread detail ───────────────────────────────────────────────────

    def get_thread(self, thread_id: str, reply_count: int = 20) -> dict[str, Any]:
        """A full thread with replies. *thread_id* is a node id or database id.

        *reply_count* is clamped to the API maximum of 20.
        """
        thread_id = to_node_id("Thread", thread_id)
        variables = {
            "threadId": thread_id,
            "sortRepliesBy": "UPVOTE_RANK_THEN_CREATED_AT",
            "includeHiddenForNetworkInDiscovery": True,
            "includeSenderBadges": True,
            "includeOriginNetworkBadge": True,
            "includeUserHideFields": True,
            "includeViewerHasBookmarked": True,
            "replyCount": min(reply_count, MAX_REPLIES),
            "includeModerationState": True,
            "includeMessageContentSourceFile": True,
            "includeVerifiedReply": True,
            "includeViewerCanPrivateReply": True,
            "requestContentInTargetLanguage": True,
            "contentTargetLanguage": "en-us",
        }
        data = self._gql("NestedThreadClients", variables)
        thread = data.get("thread")
        if not thread:
            raise LookupError(f"thread not found or query rejected by the API: {thread_id}")
        return format_thread(thread)

    # ── Inbox ───────────────────────────────────────────────────────────

    def get_inbox(self, count: int = 10, inbox_type: str = "UNREAD") -> dict[str, Any]:
        """Inbox threads (UNREAD or ALL). The unread count is fetched separately.

        *count* is clamped to the API page maximum of 20 (no proven cursor).
        """
        variables = {
            "threadCount": min(count, MAX_PAGE),
            "replyCount": 2,
            "sortRepliesBy": "CREATED_AT",
            "requestContentInTargetLanguage": True,
            "contentTargetLanguage": "en-us",
            "inboxFeedType": inbox_type,
            "includeViewerHasBookmarked": True,
            "includeOriginNetworkBadge": True,
            "includeUserHideFields": True,
            "includeVerifiedReply": True,
        }
        data = self._gql("InboxFeedQuery", variables)
        inbox = data.get("viewer", {}).get("inbox", {})
        threads_data = inbox.get("threads", {})
        edges = threads_data.get("edges", [])
        return {
            "unreadCount": self.get_inbox_unread_count(),
            "threads": [format_thread(e["node"]) for e in edges if e.get("node")],
            "pageInfo": threads_data.get("pageInfo"),
        }

    def get_inbox_unread_count(self) -> int:
        """The inbox unread-thread count."""
        data = self._gql("InboxUnreadCountClients")
        inbox = data.get("viewer", {}).get("inbox", {})
        unread_info = inbox.get("inboxUnreadCount", {})
        if isinstance(unread_info, dict):
            return len(unread_info.get("unreadThreadsInfo", []))
        return unread_info if isinstance(unread_info, int) else 0

    # ── Search ──────────────────────────────────────────────────────────

    def search(self, query: str, count: int = 10, group_scoped: bool = False) -> dict[str, Any]:
        """Search threads by text."""
        variables = {
            "searchText": query,
            "count": count,
            "isGroupScopedSearch": group_scoped,
            "includeOriginNetworkBadge": True,
            "includeUserHideFields": True,
            "locale": "en-us",
            "viewerMentionedFilter": False,
        }
        data = self._gql("SearchClients", variables)
        threads = data.get("search", {}).get("threads", {})
        edges = threads.get("edges", [])
        return {
            "threads": [format_thread(e["node"]) for e in edges if e.get("node")],
            "pageInfo": threads.get("pageInfo"),
        }

    def search_groups(self, query: str) -> list[dict[str, Any]]:
        """Search communities/groups by name."""
        variables: dict[str, Any] = {
            "searchText": query,
            "networkIds": [],
            "networkScopeType": "DEFAULT",
            "resultFilters": None,
        }
        data = self._gql("SearchResultsGroupQuery", variables)
        groups = data.get("search", {}).get("groups") or data.get("searchGroups") or {}
        edges = groups.get("edges", [])
        return [format_group(e["node"]) for e in edges if e.get("node")]

    # ── Communities ─────────────────────────────────────────────────────

    def get_my_communities(self, count: int = 20) -> list[dict[str, Any]]:
        """Communities the current user has joined."""
        variables = {"groupsCount": count, "includeFavorites": False, "locale": "en-us"}
        data = self._gql("MyGroupsClients", variables)
        edges = data.get("viewer", {}).get("groups", {}).get("edges", [])
        return [format_group(e["node"]) for e in edges if e.get("node")]

    def get_official_communities(self, count: int = 20) -> list[dict[str, Any]]:
        """Official (company-endorsed) communities."""
        variables = {"groupsCount": count, "featuredMembersCount": 6}
        data = self._gql("OfficialGroupsClients", variables)
        edges = data.get("officialGroups", {}).get("edges", [])
        return [format_group(e["node"]) for e in edges if e.get("node")]

    def get_suggested_communities(self, count: int = 20) -> list[dict[str, Any]]:
        """Suggested communities for the current user."""
        variables = {
            "groupsCount": count,
            "featuredMembersCount": 4,
            "rankedBy": "LEGACY_PROXIMITY_RANK",
            "surface": "WEB_DISCOVER_GROUPS",
        }
        data = self._gql("SuggestedGroupsClients", variables)
        edges = data.get("viewer", {}).get("suggestedGroups", {}).get("edges", [])
        return [format_group(e["node"]) for e in edges if e.get("node")]

    def get_community_info(self, group_id: str) -> dict[str, Any]:
        """Detailed info about a community. *group_id* is a node id or database id."""
        group_id = encode_id("Group", group_id) if group_id.isdigit() else group_id
        variables = {
            "groupId": group_id,
            "databaseId": decode_id(group_id)["id"],
            "includeGroupFeaturePermissions": True,
        }
        data = self._gql("GroupHeaderClients", variables)
        group = data.get("group")
        if not group:
            raise LookupError(f"community not found or not accessible: {group_id}")
        network = group.get("network", {})
        result = format_group(group)
        result.update(
            {
                "membersCount": group.get("membersCount"),
                "adminsCount": group.get("adminsCount"),
                "createdAt": group.get("createdAt"),
                "network": network.get("displayName") if network else None,
                "isDynamic": group.get("isDynamicMembership"),
                "isAllCompany": group.get("isAllCompanyGroup"),
            }
        )
        return result

    def get_community_members(self, group_id: str, role: str = "ALL") -> list[dict[str, Any]]:
        """Members of a community. *role* is ALL, ADMIN, or MEMBER.

        The ad-hoc field exposes no role/count arguments, so the role filter is
        not applied server-side and the page size is a fixed 50; the CLI warns
        when a filter is requested.
        """
        group_id = encode_id("Group", group_id) if group_id.isdigit() else group_id
        variables = {
            "groupId": group_id,
            "databaseId": decode_id(group_id)["id"],
            "roleFilter": role,
            "includeOriginNetworkBadge": False,
        }
        data = self._gql("GroupRoleMembersClients", variables)
        group = data.get("group")
        if not group:
            raise LookupError(f"community not found or not accessible: {group_id}")
        members_data = group.get("roleMembers") or group.get("members") or {}
        edges = members_data.get("edges", [])
        return [
            {
                "userId": (m.get("node") or {}).get("id"),
                "displayName": (m.get("node") or {}).get("displayName"),
                "email": (m.get("node") or {}).get("email"),
                "jobTitle": (m.get("node") or {}).get("jobTitle"),
                "role": m.get("role"),
            }
            for m in edges
            if m.get("node")
        ]

    def get_navigation_groups(self) -> dict[str, Any]:
        """The user's sidebar community list (joined + favorites)."""
        data = self._gql("NavigationGroupsClients")
        viewer = data.get("viewer", {})
        groups = viewer.get("groups", {}).get("edges", [])
        favorites = viewer.get("favoriteGroups", {}).get("edges", [])
        return {
            "groups": [
                {
                    "name": (e.get("node") or {}).get("displayName"),
                    "id": (e.get("node") or {}).get("id"),
                    "databaseId": (e.get("node") or {}).get("databaseId"),
                    "hasUnread": bool(
                        (e.get("node") or {})
                        .get("feed", {})
                        .get("unseenThreads", {})
                        .get("threads")
                    ),
                }
                for e in groups
                if e.get("node")
            ],
            "favorites": [
                {
                    "name": (e.get("node") or {}).get("displayName"),
                    "id": (e.get("node") or {}).get("id"),
                }
                for e in favorites
                if e.get("node")
            ],
        }

    # ── Notifications ───────────────────────────────────────────────────

    def get_notifications(self, count: int = 10) -> list[dict[str, Any]]:
        """Recent notifications (reaction/message previews), at most *count*.

        The ad-hoc notifications field exposes no page-size argument (the server
        returns its default page), so *count* is enforced client-side.
        """
        variables = {"pageSize": count, "before": None}
        data = self._gql("NotificationListInitialQuery", variables)
        edges = data.get("viewer", {}).get("notifications", {}).get("edges", [])
        edges = edges[: max(count, 0)]
        results = []
        for edge in edges:
            node = edge.get("node", {})
            details = node.get("details") or {}
            thread = details.get("thread") or {}
            messages = details.get("messages") or []
            notif: dict[str, Any] = {
                "id": node.get("id"),
                "type": details.get("__typename"),
                "createdAt": node.get("createdAt"),
                "isUnseen": node.get("isUnseen"),
            }
            if thread:
                starter = thread.get("threadStarter") or {}
                notif["threadId"] = thread.get("id")
                notif["threadText"] = extract_plain_text(starter)[:200]
                group = thread.get("group") or {}
                if group:
                    notif["group"] = group.get("displayName")
            if details.get("reactingUsersCount"):
                notif["reactingUsersCount"] = details["reactingUsersCount"]
                reactions = details.get("featuredReactions") or []
                if reactions:
                    notif["reactions"] = [
                        {
                            "type": r.get("reaction", ""),
                            "user": (r.get("user") or {}).get("displayName", ""),
                        }
                        for r in reactions
                    ]
            if messages:
                notif["messages"] = [
                    {
                        "sender": (msg.get("sender") or {}).get("displayName"),
                        "text": extract_plain_text(msg)[:200],
                    }
                    for msg in messages[:3]
                ]
            results.append(notif)
        return results

    # ── Write operations ────────────────────────────────────────────────

    def post_message(
        self,
        group_id: str,
        text: str,
        is_announcement: bool = False,
        is_question: bool = False,
        topic_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Post a thread starter to a community. *group_id* is a node id or database id."""
        group_id = to_node_id("Group", group_id)
        variables = {
            "isDraft": False,
            "groupId": group_id,
            "serializedContentState": build_draftjs_content(text),
            "notifiedUserIds": [],
            "attachmentIds": [],
            "topicNames": topic_names,
            "officeTopicAuthorizationTokens": [],
            "isQuestion": is_question,
            "isArticle": False,
            "isAnnouncement": is_announcement,
            "clientMutationId": str(uuid.uuid4()),
            "isMandatoryNotification": False,
            "sortRepliesBy": "UPVOTE_RANK_THEN_CREATED_AT",
            "includeSenderBadges": True,
            "includeVerifiedReply": True,
            "requestContentInTargetLanguage": True,
            "contentTargetLanguage": "en-us",
        }
        data = self._gql("PublishGroupMessageClients", variables)
        thread = data.get("createGroupMessage", {}).get("message", {}).get("thread", {})
        thread_id = thread.get("id")
        return {
            "threadId": thread_id,
            "databaseId": thread.get("databaseId"),
            "messageId": thread_id,  # the thread-starter message id equals the thread id
            "group": thread.get("group", {}).get("displayName"),
        }

    def post_to_storyline(
        self, text: str, is_question: bool = False, topic_names: list[str] | None = None
    ) -> dict[str, Any]:
        """Post to the current user's own Storyline feed (a distinct mutation)."""
        user_id = self.get_current_user().get("userId")
        variables = {
            "isDraft": False,
            "userId": user_id,
            "serializedContentState": build_draftjs_content(text),
            "notifiedUserIds": [],
            "attachmentIds": [],
            "topicNames": topic_names,
            "officeTopicAuthorizationTokens": [],
            "isQuestion": is_question,
            "isArticle": False,
            "isUserMoment": False,
            "clientMutationId": str(uuid.uuid4()),
            "includeThreadLevelAndParentId": False,
            "includeImageArtifactFields": False,
            "includeSharePointNewsPost": False,
            "requestContentInTargetLanguage": True,
            "contentTargetLanguage": "en-us",
        }
        data = self._gql("PublishUserMessageClients", variables)
        # The mutation payload wrapper key varies; find the one carrying a message.
        payload: dict[str, Any] = {}
        for v in data.values():
            if isinstance(v, dict) and v.get("message"):
                payload = v
                break
        message = payload.get("message", {})
        thread = message.get("thread", {}) or {}
        thread_id = thread.get("id") or message.get("id")
        return {
            "threadId": thread_id,
            "databaseId": thread.get("databaseId"),
            "messageId": message.get("id") or thread_id,
            "storyline": True,
        }

    def reply_to_thread(self, message_id: str, text: str) -> dict[str, Any]:
        """Reply to a thread by the id of the message replied to (usually the starter)."""
        message_id = to_node_id("Message", message_id)
        variables = {
            "serializedContentState": build_draftjs_content(text),
            "replyToMessageMutationId": message_id,
            "isSecondLevelReply": False,
            "notifiedUserIds": [],
            "attachmentIds": [],
            "clientMutationId": str(uuid.uuid4()),
            "includeSenderBadges": True,
            "includeOriginNetworkBadge": True,
            "isModeratorMessage": False,
            "isAnonymousMessage": False,
            "isPrivateReply": False,
        }
        data = self._gql("PublishReplyMessageClients", variables)
        msg = data.get("replyMessage", {}).get("message", {})
        return {
            "messageId": msg.get("id"),
            "threadId": msg.get("thread", {}).get("id"),
            "sender": msg.get("sender", {}).get("displayName"),
            "text": extract_plain_text(msg),
        }

    def react_to_message(self, message_id: str, reaction: str = "LIKE") -> dict[str, Any]:
        """Add a reaction to a message. Only ``LIKE`` is verified against live traffic."""
        message_id = to_node_id("Message", message_id)
        variables = {"messageId": message_id, "reaction": reaction}
        data = self._gql("LikeClients", variables)
        msg = data.get("addMessageReaction", {}).get("message", {})
        return {"messageId": msg.get("id"), "reaction": reaction}
