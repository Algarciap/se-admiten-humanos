"use strict";

var SourceRegistry = (function () {
    var sourceDomains = {
        "idealista.com": "idealista",
        "fotocasa.es": "fotocasa",
        "pisos.com": "pisos",
        "yaencontre.com": "yaencontre",
        "habitaclia.com": "habitaclia",
        "alquilerseguro.es": "alquiler_seguro",
        "agencianegociadoradelalquiler.es": "agencia_negociadora_alquiler",
        "testahomes.com": "testa_homes",
        "vivenio.com": "vivenio",
        "nestarhomes.es": "nestar",
        "encajatualquiler.com": "fidere",
        "vivetuavalon.com": "avalon",
        "tecnocasa.es": "tecnocasa",
        "redpiso.es": "redpiso",
        "gilmar.es": "gilmar",
        "engelvoelkers.com": "engel_voelkers",
        "remax.es": "remax",
        "aproperties.es": "aproperties",
        "vivienda2.com": "vivienda2",
        "promora.com": "promora"
    };

    function sourceIdFromText(text) {
        var normalized = String(text || "").toLowerCase();
        var domains = Object.keys(sourceDomains);
        for (var i = 0; i < domains.length; i += 1) {
            if (normalized.indexOf(domains[i]) !== -1) {
                return sourceDomains[domains[i]];
            }
        }
        return "unknown";
    }

    function allowedListingUrl(url) {
        var match = String(url || "").match(/^https?:\/\/([^/?#]+)/i);
        if (!match) {
            return false;
        }
        var host = match[1]
            .split("@")
            .pop()
            .split(":")[0]
            .toLowerCase()
            .replace(/^www\./, "");
        return Object.keys(sourceDomains).some(function (domain) {
            return host === domain || host.slice(-(domain.length + 1)) === "." + domain;
        });
    }

    return {
        allowedListingUrl: allowedListingUrl,
        sourceIdFromText: sourceIdFromText
    };
})();

if (typeof module !== "undefined" && module.exports) {
    module.exports = SourceRegistry;
}
