"""Hosts by what they are: news outlets, social platforms, press-release wires and blog hosts.

Named by hand, one group at a time. An outlet is on the list because it is an
established one (a wire service, a public broadcaster, a paper of record or a major
daily, the business and tech press), never because a site's name sounds like news: a
site that is not named here is ``other``, whatever it calls itself. Tabloids and
state-run propaganda outlets are left out. A host matches itself and its subdomains
(``news.sky.com`` is one entry, ``sky.com`` would be every page of Sky).
"""

from __future__ import annotations

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
