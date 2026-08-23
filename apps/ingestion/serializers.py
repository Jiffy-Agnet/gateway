"""Serializers for ingestion endpoints."""

from rest_framework import serializers


class RepoSerializer(serializers.Serializer):
    url = serializers.URLField(max_length=500, help_text="Git repository URL")
    token = serializers.CharField(help_text="Short-lived repo access token for clone/push/PR operations")
    username = serializers.CharField(
        max_length=100,
        required=False,
        default="",
        help_text="Git provider username (required for GitLab/Gitea, ignored for GitHub)",
    )


class TurnSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=["user", "agent"], help_text="Who authored this turn: 'user' (human) or 'agent' (Jiffy bot)")
    author = serializers.CharField(max_length=200, help_text="Login/username of the turn author")
    body = serializers.CharField(
        allow_blank=True,
        # No max_length and no trimming: the turn body is the verbatim issue or
        # comment text and is handed to the agent as-is. Threads can run to
        # megabytes — nothing here may cap or reshape them.
        trim_whitespace=False,
        help_text="The text content of this turn (may be blank — GitHub issues and comments can have an empty body)",
    )
    created_at = serializers.DateTimeField(help_text="ISO-8601 timestamp of when this turn was created")


class IssueSerializer(serializers.Serializer):
    # Same as TurnSerializer.body: unbounded and untrimmed, so the legacy
    # single-blob form reaches the agent exactly as it was sent.
    text = serializers.CharField(
        required=False,
        trim_whitespace=False,
        help_text="Full issue/thread text for the coding agent (legacy; prefer 'turns')",
    )
    turns = TurnSerializer(many=True, required=False, help_text="Ordered array of conversation turns with role/author metadata")
    external_issue_id = serializers.CharField(max_length=100, help_text="External issue/thread ID from the git provider")

    def validate(self, data):
        # ``text`` is no longer trimmed, so a whitespace-only blob must still
        # count as missing here.
        if not (data.get("text") or "").strip() and not data.get("turns"):
            raise serializers.ValidationError("Either 'text' or 'turns' must be provided in the issue object")
        return data


class CallbackSerializer(serializers.Serializer):
    url = serializers.URLField(max_length=500, help_text="URL to POST the final result to")
    secret = serializers.CharField(max_length=1024, help_text="Opaque secret passed through to the callback endpoint")


class IngestionPayloadSerializer(serializers.Serializer):
    repo = RepoSerializer()
    issue = IssueSerializer()
    callback = CallbackSerializer()
