"""Step-by-step guides for common content workflows (daisy-content#22).

One source, served two ways:

- as MCP prompts (`content-build-landing-page`, ...), for a client that
  talks to this server directly and lists prompts;
- through the `content_get_workflow` tool, because the DAISY gateway proxies
  tools only (`list_available_tools` / `execute_tool`), so a Cowork session
  reaching this server through the gateway never sees MCP prompts.

The guides are static text written against the pinned module spec and the
daisy-content#10 testbed's behaviour; each names the tools and arguments to use, in
order. They are intentionally short and point at the tools' own
descriptions and `content_schema_class` for detail.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

# No `from __future__ import annotations` here: FastMCP builds each prompt's
# argument model from these functions' annotations, and string annotations
# can't be resolved from its namespace.


@dataclass(frozen=True)
class Workflow:
    name: str
    title: str
    description: str
    # Argument name -> description; every argument is optional.
    arguments: dict[str, str]
    render: Callable[..., str]


_SITE_DESCRIPTION = "Site slug (optional with one configured site)."
_PAGE_DESCRIPTION = "The page's URL segment, title or id."
_BRIEF_DESCRIPTION = "What the page is for and what it should say."

# `str` with an empty default, not Optional[str], and the description in
# Annotated so it reaches prompt clients. FastMCP still appends a "Provide as
# a JSON string matching ..." hint after it for any annotation other than
# bare `str`; a plain string value is accepted either way.
SiteArg = Annotated[str, Field(description=_SITE_DESCRIPTION)]
PageArg = Annotated[str, Field(description=_PAGE_DESCRIPTION)]
BriefArg = Annotated[str, Field(description=_BRIEF_DESCRIPTION)]


def _site_line(site: str | None) -> str:
    if site:
        return f'Target site: "{site}". Pass site="{site}" on every content_* call.'
    return (
        "Target site: call content_list_sites if you don't know the slug; `site` "
        "may be omitted only when exactly one site is configured."
    )


def _build_landing_page(site: SiteArg = "", brief: BriefArg = "") -> str:
    brief_line = f"\nBrief: {brief}\n" if brief else ""
    return f"""Build a landing page on a SilverStripe site through the content API.
{_site_line(site)}{brief_line}
1. content_get_started: read `warnings` first. If populationEnabled is false,
   compositions are closed on this environment (403 ENV_FORBIDDEN); stop and
   say so. Pick a page class from classes.pages that has an Elemental area
   (on an Essentials site, BlockPage) and element classes from
   classes.elements.
2. content_schema_class for the page class and each element class you plan
   to use. Only fields marked writable are accepted; anything else is a 422
   UNKNOWN_FIELD / READONLY_FIELD naming the field.
3. Plan the sections as an ordered list. Give each element a stable,
   readable externalId (for example "spring-open-house-hero"): it is the
   key that makes a repeat call update the element instead of adding a copy.
4. content_compose_page with publish "none":
   page.match {{"urlSegment": "<slug>"}} plus page.createIfMissing
   {{"title": "...", "parentId": <id of an existing parent page, 0 for top
   level>, "className": "<page class>"}}, and elements in display order
   (array order sets Sort). Images go in `assets` with a `ref`, used in an
   element's fields as {{"$ref": "<ref>"}}.
5. Review: content_records_read on the page, or open its draft in the CMS.
   Fix anything by re-sending the same composition with changes; it is
   idempotent on externalId.
6. Publish when the user approves: the same composition with publish
   "recursive", or content_records_stage on the page with action "publish"
   and mode "owns". Not mode "recursive": that publishes the page but not
   its elements, which stay draft behind a live page.
   Confirm with content_records_parity on the page.
"""


def _add_section(site: SiteArg = "", page: PageArg = "") -> str:
    target = f'the page "{page}"' if page else "an existing page"
    return f"""Add one section to {target} without disturbing the sections already there.
{_site_line(site)}
1. Find the page: content_records_list on its class with
   filters {{"URLSegment": "<slug>"}} (or content_records_read by id). Note its
   id and classRef.
2. Find the existing sections: content_records_read on the page;
   relations.ElementalArea is the area id. If the page's relations also
   include ElementalHomePage (an Essentials home page), that is the area the
   page renders: use its id here and in step 4. Then content_records_list on
   each element class with filters {{"ParentID": <area id>}} and
   fields ["Sort"] to see their order and externalIds.
3. content_schema_class on the element class you are adding.
4. content_compose_page with page.match {{"id": <page id>}} (plus
   page.areaRelation "ElementalHomePage" for an Essentials home page) and an
   elements array holding only the new element, with a new externalId. Leave `prune`
   out: without it, elements missing from the payload stay where they are.
   Array order sets Sort, so a lone element gets Sort 1 and lands first.
   To place it elsewhere, set fields.Sort explicitly (check that
   content_schema_class lists Sort as writable), e.g. one more than the
   current highest to append it.
5. Review the draft, then publish when approved: re-send the same
   one-element composition with publish "recursive", or content_records_stage
   on the page with action "publish" and mode "owns" (mode "recursive" leaves
   the elements in draft).
Never re-send a composition with prune enabled unless the user asked to
remove sections: prune archives managed elements missing from the payload.
"""


def _populate_from_brief(site: SiteArg = "", brief: BriefArg = "") -> str:
    brief_line = f"\nBrief: {brief}\n" if brief else ""
    return f"""Populate a page from a brief, with generated images.
{_site_line(site)}{brief_line}
1. content_get_started, then content_schema_class for the page class and the
   element classes the brief needs (an image field shows as a has_one to an
   Image class).
2. Write the copy for each section from the brief. Keep each field within
   what content_schema_class describes; rich text fields take HTML.
3. Images: generate or fetch each one (for example with the DAISY media
   tools), and pass the bytes as base64 in content_compose_page's `assets`,
   each with a `ref`, a `filename` and a `title` (used as the image title).
   Reference them from element fields as {{"$ref": "<ref>"}}. Large images
   are better uploaded one at a time with content_asset_upload first, passing
   publish: false so the image stays draft until the page is approved (it
   publishes by default); then reference the returned id.
4. content_compose_page with publish "none" and stable externalIds on every
   element (see the landing-page workflow, steps 3 to 6, for review and
   publish).
"""


def _check_what_changed(site: SiteArg = "") -> str:
    return f"""Check what a content change did, before and after.
{_site_line(site)}
1. Before the change: content_fingerprint (response_format "concise" for
   counts only, or the default for every row; pass `classes` to limit the
   sections). Keep the result.
2. Make the change.
3. After: content_fingerprint again with the same arguments and compare.
   Rows are keyed by URL path, not id. `violations` lists live pages whose
   path runs through a non-live ancestor; those 404 on the live site.
4. For one page: content_records_parity on it reports the page's
   field-level draft/live differences and whether each record it owns is
   live.
5. For a planned multi-record change, run content_batch with dryRun: true
   first: it validates and authorizes every operation, then rolls back.
"""


WORKFLOWS: dict[str, Workflow] = {
    w.name: w
    for w in (
        Workflow(
            name="build-landing-page",
            title="Build a landing page",
            description=(
                "Discover the site, pick a page type, compose its sections, "
                "review, then publish."
            ),
            arguments={
                "site": _SITE_DESCRIPTION,
                "brief": _BRIEF_DESCRIPTION,
            },
            render=_build_landing_page,
        ),
        Workflow(
            name="add-section",
            title="Add a section to an existing page",
            description=("Add one element to a page without disturbing the others."),
            arguments={
                "site": _SITE_DESCRIPTION,
                "page": _PAGE_DESCRIPTION,
            },
            render=_add_section,
        ),
        Workflow(
            name="populate-from-brief",
            title="Populate a page from a brief",
            description=(
                "Turn a brief and generated images into a composed draft page."
            ),
            arguments={
                "site": _SITE_DESCRIPTION,
                "brief": _BRIEF_DESCRIPTION,
            },
            render=_populate_from_brief,
        ),
        Workflow(
            name="check-what-changed",
            title="Check what changed",
            description=("Fingerprint before and after a change; parity for one page."),
            arguments={"site": _SITE_DESCRIPTION},
            render=_check_what_changed,
        ),
    )
}


def list_workflows() -> list[dict]:
    return [
        {
            "name": w.name,
            "title": w.title,
            "description": w.description,
            "arguments": w.arguments,
        }
        for w in WORKFLOWS.values()
    ]


def get_workflow(name: str, **arguments: str | None) -> dict:
    """One workflow's text, rendered with whichever arguments were given."""
    workflow = WORKFLOWS.get(name)
    if workflow is None:
        raise KeyError(name)
    unknown = set(arguments) - set(workflow.arguments)
    if unknown:
        raise TypeError(", ".join(sorted(unknown)))
    given = {k: v for k, v in arguments.items() if v}
    return {
        "name": workflow.name,
        "title": workflow.title,
        "text": workflow.render(**given),
    }
