"""Hosts by what they are: news outlets, social platforms, press-release wires and blog hosts.

Named by hand, one group at a time. An outlet is on the list because it is an
established one (a wire service, a public broadcaster, a paper of record or a major
daily, the business and tech press), never because a site's name sounds like news: a
site that is not named here is ``other``, whatever it calls itself. Tabloids and
state-run propaganda outlets are left out. A host matches itself and its subdomains
(``news.sky.com`` is one entry, ``sky.com`` would be every page of Sky).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# Wire services and news agencies.
_WIRE_SERVICES = frozenset(
    {
        "afp.com",
        "ansa.it",
        "apnews.com",
        "bloomberg.com",
        "dpa.com",
        "efe.com",
        "kyodonews.net",
        "ntb.no",
        "pap.pl",
        "ptinews.com",
        "reuters.com",
        "ritzau.dk",
        "stt.fi",
        "tt.se",
        "upi.com",
        "yna.co.kr",
        "aa.com.tr",  # Anadolu Agency
    }
)

# National and public broadcasters.
_BROADCASTERS = frozenset(
    {
        "abc.net.au",
        "bbc.co.uk",
        "bbc.com",  # BBC Türkçe, BBC Persian and the rest live under bbc.com
        "c-span.org",
        "cbc.ca",
        "channelnewsasia.com",
        "dr.dk",
        "dw.com",
        "err.ee",
        "euronews.com",
        "france24.com",
        "franceinfo.fr",
        "npr.org",
        "nhk.or.jp",
        "nos.nl",
        "nrk.no",
        "orf.at",
        "pbs.org",
        "radio-canada.ca",
        "rfi.fr",
        "rnz.co.nz",
        "rte.ie",
        "rts.ch",
        "ruv.is",
        "sr.se",
        "srf.ch",
        "svt.se",
        "tagesschau.de",
        "trthaber.com",
        "trt.net.tr",
        "trtworld.com",
        "vrt.be",
        "vrtnws.be",
        "yle.fi",  # Finland's public broadcaster, svenska.yle.fi included
        "aljazeera.com",
        "aljazeera.net",
    }
)

# United States.
_UNITED_STATES = frozenset(
    {
        "abcnews.go.com",
        "axios.com",
        "bostonglobe.com",
        "cbsnews.com",
        "chicagotribune.com",
        "cnn.com",
        "democracynow.org",
        "foxnews.com",
        "latimes.com",
        "msnbc.com",
        "nbcnews.com",
        "newsweek.com",
        "newyorker.com",
        "nytimes.com",
        "politico.com",
        "propublica.org",
        "semafor.com",
        "theatlantic.com",
        "theintercept.com",
        "thehill.com",
        "time.com",
        "usatoday.com",
        "vox.com",
        "washingtonpost.com",
        "wsj.com",
    }
)

# United Kingdom and Ireland.
_UNITED_KINGDOM_IRELAND = frozenset(
    {
        "economist.com",
        "ft.com",
        "independent.co.uk",
        "independent.ie",
        "inews.co.uk",
        "irishtimes.com",
        "news.sky.com",
        "standard.co.uk",
        "telegraph.co.uk",
        "theguardian.com",
        "thejournal.ie",
        "thetimes.co.uk",
    }
)

# The European Union: Brussels, France, Germany, the Low Countries, Italy, Iberia, the
# German-speaking and Central European press.
_EUROPE = frozenset(
    {
        "berliner-zeitung.de",
        "derstandard.at",
        "diepresse.com",
        "elmundo.es",
        "elpais.com",
        "euractiv.com",
        "euobserver.com",
        "expresso.pt",
        "faz.net",
        "handelsblatt.com",
        "ilsole24ore.com",
        "kathimerini.gr",
        "lastampa.it",
        "lavanguardia.com",
        "lefigaro.fr",
        "lemonde.fr",
        "lesechos.fr",
        "lesoir.be",
        "liberation.fr",
        "nrc.nl",
        "nzz.ch",
        "politico.eu",
        "publico.pt",
        "repubblica.it",
        "corriere.it",
        "spiegel.de",
        "standaard.be",
        "sueddeutsche.de",
        "volkskrant.nl",
        "welt.de",
        "wyborcza.pl",
        "zeit.de",
    }
)

# The Nordics and the Baltics. Finland: Helsingin Sanomat, the business and regional papers.
_NORDICS_BALTICS = frozenset(
    {
        "aamulehti.fi",
        "aftenposten.no",
        "berlingske.dk",
        "borsen.dk",
        "di.se",
        "dn.no",
        "dn.se",
        "e24.no",
        "gp.se",
        "hbl.fi",
        "hs.fi",
        "information.dk",
        "jyllands-posten.dk",
        "kaleva.fi",
        "kauppalehti.fi",
        "mtvuutiset.fi",
        "omni.se",
        "politiken.dk",
        "postimees.ee",
        "svd.se",
        "talouselama.fi",
        "tekniikkatalous.fi",
        "tivi.fi",
        "ts.fi",
    }
)

# Turkey: the agency and the public broadcaster above, the main dailies and the news channels.
_TURKEY = frozenset(
    {
        "bianet.org",
        "birgun.net",
        "bloomberght.com",
        "cnnturk.com",
        "cumhuriyet.com.tr",
        "dailysabah.com",
        "diken.com.tr",
        "duvarenglish.com",
        "dunya.com",
        "haberturk.com",
        "hurriyet.com.tr",
        "hurriyetdailynews.com",
        "milliyet.com.tr",
        "ntv.com.tr",
        "sabah.com.tr",
        "sozcu.com.tr",
        "t24.com.tr",
        "turkishminute.com",
    }
)

# Asia and the Pacific.
_ASIA_PACIFIC = frozenset(
    {
        "afr.com",
        "asahi.com",
        "bangkokpost.com",
        "business-standard.com",
        "dawn.com",
        "hindustantimes.com",
        "indianexpress.com",
        "japantimes.co.jp",
        "koreaherald.com",
        "koreatimes.co.kr",
        "livemint.com",
        "mainichi.jp",
        "ndtv.com",
        "nikkei.com",
        "nzherald.co.nz",
        "scmp.com",
        "smh.com.au",
        "straitstimes.com",
        "thehindu.com",
        "theage.com.au",
        "thejakartapost.com",
        "yomiuri.co.jp",
    }
)

# Canada, Latin America, the Middle East, Africa and Eastern Europe beyond the Union.
_ELSEWHERE = frozenset(
    {
        "arabnews.com",
        "clarin.com",
        "ctvnews.ca",
        "dailymaverick.co.za",
        "eltiempo.com",
        "eluniversal.com.mx",
        "estadao.com.br",
        "folha.uol.com.br",
        "globalnews.ca",
        "haaretz.com",
        "infobae.com",
        "kyivindependent.com",
        "lanacion.com.ar",
        "latercera.com",
        "meduza.io",
        "mg.co.za",
        "nationalpost.com",
        "news24.com",
        "thenationalnews.com",
        "theglobeandmail.com",
        "themoscowtimes.com",
        "thestar.com",
        "timesofisrael.com",
    }
)

# The major business and technology press.
_BUSINESS_TECH = frozenset(
    {
        "arstechnica.com",
        "barrons.com",
        "businessinsider.com",
        "cnbc.com",
        "cnet.com",
        "engadget.com",
        "fastcompany.com",
        "forbes.com",
        "fortune.com",
        "gizmodo.com",
        "marketwatch.com",
        "restofworld.org",
        "sifted.eu",
        "techcrunch.com",
        "technologyreview.com",
        "theinformation.com",
        "theregister.com",
        "theverge.com",
        "thenextweb.com",
        "tomshardware.com",
        "venturebeat.com",
        "wired.com",
        "zdnet.com",
    }
)

NEWS_HOSTS = frozenset().union(
    _WIRE_SERVICES,
    _BROADCASTERS,
    _UNITED_STATES,
    _UNITED_KINGDOM_IRELAND,
    _EUROPE,
    _NORDICS_BALTICS,
    _TURKEY,
    _ASIA_PACIFIC,
    _ELSEWHERE,
    _BUSINESS_TECH,
)

# Posts and videos: a post is not the story.
SOCIAL_HOSTS = frozenset(
    {
        "bsky.app",
        "facebook.com",
        "fb.com",
        "fb.watch",
        "instagram.com",
        "linkedin.com",
        "pinterest.com",
        "t.me",
        "threads.com",
        "threads.net",
        "tiktok.com",
        "twitter.com",
        "vk.com",
        "weibo.com",
        "x.com",
    }
)
# Video sites that are social only when the query asks for news: a tutorial on YouTube is
# what was asked for, a video about the news is not the story.
SOCIAL_ON_NEWS_HOSTS = frozenset({"youtu.be", "youtube.com"})

# Press-release wires: the company's own announcement, paid for, not reporting.
PRESS_RELEASE_HOSTS = frozenset(
    {
        "24-7pressrelease.com",
        "accessnewswire.com",
        "accesswire.com",
        "businesswire.com",
        "cision.com",
        "einnews.com",
        "einpresswire.com",
        "eqs-news.com",
        "globenewswire.com",
        "issuewire.com",
        "marketwired.com",
        "mynewsdesk.com",
        "newsfilecorp.com",
        "newswire.ca",
        "newswire.com",
        "openpr.com",
        "ots.at",
        "pr.com",
        "presseportal.de",
        "prlog.org",
        "prnewswire.com",
        "prweb.com",
        "send2press.com",
        "sttinfo.fi",
    }
)

# Blog hosts: anyone can publish there, so a page on one is a community post.
BLOG_HOSTS = frozenset(
    {
        "bearblog.dev",
        "beehiiv.com",
        "blogger.com",
        "blogspot.com",
        "ghost.io",
        "livejournal.com",
        "substack.com",
        "tumblr.com",
        "typepad.com",
        "wordpress.com",
        "write.as",
    }
)


# --- first-party sites and aggregators ------------------------------------------------------------------
#
# An organization's own site speaks for the organization: what OpenAI says at openai.com, Google
# at cloud.google.com or blog.google, Apple in its newsroom, is the primary source of its news, and
# an outlet that reports it is the second. An entry is a host (it matches itself and its
# subdomains) or a host with a path (``apple.com/newsroom``: only that part of the site). A
# community or forum subdomain of a first-party host (``community.openai.com``) is not the
# organization speaking and is left out. A site's own table is added to this one with
# WEB_TOOLS_OFFICIAL_HOSTS.
FIRST_PARTY_HOSTS = frozenset(
    {
        # AI labs and platforms
        "openai.com",
        "anthropic.com",
        "claude.com",
        "mistral.ai",
        "x.ai",
        "cohere.com",
        "stability.ai",
        "perplexity.ai",
        "huggingface.co",
        "deepseek.com",
        # Google
        "cloud.google.com",
        "blog.google",
        "deepmind.google",
        "research.google",
        "ai.google",
        "about.google",
        "workspace.google.com",
        "developers.google.com",
        "googleblog.com",
        "developers.googleblog.com",
        # Meta
        "ai.meta.com",
        "about.meta.com",
        "about.fb.com",
        "engineering.fb.com",
        "ai.facebook.com",
        "developers.facebook.com",
        # Microsoft, Apple, Amazon, Nvidia and the other large vendors
        "microsoft.com",
        "apple.com/newsroom",
        "machinelearning.apple.com",
        "support.apple.com",
        "aws.amazon.com",
        "aboutamazon.com",
        "amazon.science",
        "nvidia.com",
        "ibm.com",
        "intel.com/content/www/us/en/newsroom",
        "news.samsung.com",
        "salesforce.com",
        "oracle.com/news",
        "github.blog",
        "cloudflare.com",
        "stripe.com",
    }
)
# Brand top-level domains: every site under one is the brand's own.
FIRST_PARTY_SUFFIXES = frozenset({"google", "microsoft", "apple", "amazon", "aws", "meta", "ibm"})
# Names of organizations a query says that no domain of the table spells.
ORGANIZATION_ALIASES = frozenset({"deepmind"})
# Subdomains that are a community's, not the organization's.
COMMUNITY_SUBDOMAINS = frozenset(
    {"community", "forum", "forums", "discuss", "discourse", "answers", "ideas", "chat"}
)

# Sites that collect other outlets' stories (and rewrite or frame them): a result from one is
# somebody else's reporting a hop away, and ranks below the report itself. WEB_TOOLS_AGGREGATOR_HOSTS
# adds to this list.
AGGREGATOR_HOSTS = frozenset(
    {
        "allsides.com",
        "alltop.com",
        "apple.news",
        "dailyhunt.in",
        "feedly.com",
        "flipboard.com",
        "getpocket.com",
        "ground.news",
        "inoreader.com",
        "memeorandum.com",
        "msn.com",
        "news.google.com",
        "news.yahoo.com",
        "newsbreak.com",
        "newsnow.co.uk",
        "particle.news",
        "pressreader.com",
        "smartnews.com",
        "techmeme.com",
    }
)

# Words that are a site's name and also just a word: a query that says "time" or "target" does
# not name the organization behind time.com or target.com.
GENERIC_NAMES = frozenset(
    {
        "about",
        "app",
        "apps",
        "best",
        "blog",
        "cloud",
        "code",
        "daily",
        "data",
        "docs",
        "free",
        "game",
        "games",
        "guide",
        "help",
        "info",
        "list",
        "media",
        "news",
        "online",
        "open",
        "page",
        "play",
        "post",
        "shop",
        "site",
        "store",
        "tech",
        "time",
        "times",
        "today",
        "tool",
        "tools",
        "video",
        "watch",
        "web",
        "wiki",
        "world",
    }
)
# The subdomains an organization speaks on, and the first path segments of its apex site: what a
# query that names the organization (stripe) makes of a page of its own (docs.stripe.com,
# stripe.com/docs) and not of any page of a site with the name's word in it.
FIRST_PARTY_SUBDOMAINS = frozenset(
    {
        "docs",
        "documentation",
        "developer",
        "developers",
        "dev",
        "api",
        "help",
        "support",
        "learn",
        "platform",
        "blog",
        "news",
        "newsroom",
        "press",
        "investors",
        "engineering",
        "research",
        "status",
        "ai",
        "labs",
        "about",
    }
)
FIRST_PARTY_SEGMENTS = frozenset(
    {
        "docs",
        "documentation",
        "developers",
        "developer",
        "api",
        "reference",
        "blog",
        "news",
        "newsroom",
        "press",
        "research",
        "changelog",
        "releases",
        "index",
        "announcements",
        "pricing",
        "product",
        "products",
        "support",
        "help",
    }
)
# Second-level suffixes under which the organization's name is the label before them.
_TWO_LEVEL_SUFFIXES = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "com.au",
        "co.nz",
        "co.jp",
        "co.kr",
        "co.in",
        "com.tr",
        "org.tr",
        "com.br",
        "com.cn",
        "com.mx",
    }
)


# The top-level domains an organization's own site is on, for the rule that a host named as the query's
# subject (``modelcontextprotocol.io`` for "Model Context Protocol") is the subject's own: the ones
# companies, projects and foundations use. The same name on another (``modelcontextprotocol.info``,
# ``.biz``, ``.xyz``, ``.net``...) is the kind of address a copy of a site is put on, and is not taken for
# the original.
ORGANIZATION_SUFFIXES = frozenset({"com", "org", "io", "dev", "ai", "app", "co"})


def has_organization_suffix(host: str) -> bool:
    """Whether a host ends in a top-level domain an organization's own site is on (``ORGANIZATION_SUFFIXES``,
    or ``co.uk``, ``com.au`` and the other second levels of ``com``, ``co`` and ``org``)."""
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES:
        return labels[-2] in {"com", "co", "org"}
    return labels[-1] in ORGANIZATION_SUFFIXES


def _entry_matches(entry: str, host: str, path: str) -> bool:
    entry_host, _, entry_path = entry.partition("/")
    if host != entry_host and not host.endswith("." + entry_host):
        return False
    if not entry_path:
        return True
    prefix = "/" + entry_path.strip("/")
    return path == prefix or path.startswith(prefix + "/")


def is_community_subdomain(host: str, entry_hosts: Iterable[str]) -> bool:
    """Whether *host* is ``community.<first-party host>``: a forum on an organization's domain."""
    first, _, rest = host.partition(".")
    return first in COMMUNITY_SUBDOMAINS and any(
        rest == entry or rest.endswith("." + entry) for entry in entry_hosts
    )


def registrable_label(host: str) -> str:
    """The label before a host's public suffix: ``stripe`` in ``docs.stripe.com``, ``bbc`` in
    ``news.bbc.co.uk``; under a brand top-level domain (``blog.google``) the brand itself."""
    labels = host.split(".")
    if labels[-1] in FIRST_PARTY_SUFFIXES:
        return labels[-1]
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else ""


def split_host(host: str) -> tuple[str, str]:
    """A host as ``(the labels left of the registrable domain, the registrable domain)``:
    ``("docs", "stripe.com")``, ``("", "openai.com")``, ``("news", "bbc.co.uk")``."""
    labels = host.split(".")
    keep = 3 if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES else 2
    if labels[-1] in FIRST_PARTY_SUFFIXES:
        keep = 1
    return ".".join(labels[:-keep]), ".".join(labels[-keep:])


@dataclass(frozen=True)
class SourceRules:
    """The tables that say which sites are first-party and which are aggregators; extendable."""

    first_party: frozenset[str] = FIRST_PARTY_HOSTS
    aggregators: frozenset[str] = AGGREGATOR_HOSTS

    def extended(
        self, first_party: Iterable[str] = (), aggregators: Iterable[str] = ()
    ) -> SourceRules:
        """These tables with more hosts: each a host, or a host and a path for a part of a site."""
        return SourceRules(
            self.first_party | frozenset(_clean_entry(entry) for entry in first_party if entry),
            self.aggregators | frozenset(_clean_entry(entry) for entry in aggregators if entry),
        )

    def is_first_party(self, host: str, path: str) -> bool:
        """Whether a host (and path) is on the first-party table or under a brand top-level domain."""
        if host.rsplit(".", 1)[-1] in FIRST_PARTY_SUFFIXES and "." in host:
            return True
        if not any(_entry_matches(entry, host, path) for entry in self.first_party):
            return False
        return not is_community_subdomain(host, (e.partition("/")[0] for e in self.first_party))

    def is_aggregator(self, host: str, path: str) -> bool:
        return any(_entry_matches(entry, host, path) for entry in self.aggregators)

    def organizations(self) -> frozenset[str]:
        """The names the first-party table holds, as a query would say them: ``openai``, ``google``."""
        names = {registrable_label(entry.partition("/")[0]) for entry in self.first_party}
        named = frozenset(name for name in names if len(name) >= 4 and name not in GENERIC_NAMES)
        return named | ORGANIZATION_ALIASES


def _clean_entry(entry: str) -> str:
    """``https://www.Example.com/News/`` as ``example.com/news``."""
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", entry.strip().lower())
    text = re.split(r"[?#]", text, maxsplit=1)[0].strip("/")
    return text[4:] if text.startswith("www.") else text


DEFAULT_RULES = SourceRules()
