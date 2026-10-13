"use strict";

var PetPolicy = (function () {
    var STATUS = Object.freeze({
        ACCEPTED: "accepted",
        CONDITIONAL: "conditional",
        NOT_SPECIFIED: "not_specified",
        REJECTED: "rejected"
    });

    var acceptedPatterns = [
        /(?:se\s+)?(?:admiten|aceptan|permiten)\s+(?:las?\s+)?(?:mascotas?|animales?)/i,
        /(?:mascotas?|animales?)\s+(?:admitid[oa]s?|aceptad[oa]s?|permitid[oa]s?)/i,
        /(?:pet[\s-]?friendly|pets?\s+(?:are\s+)?(?:welcome|allowed))/i,
        /(?:gatos?|perros?)\s+(?:si|sí|admitid[oa]s?|aceptad[oa]s?|permitid[oa]s?)/i,
        /no\s+(?:hay|habria|habría|existe)\s+(?:ningun\s+|ningún\s+)?problema\s+con\s+(?:las?\s+)?(?:mascotas?|animales?)/i
    ];

    var conditionalPatterns = [
        /(?:mascotas?|animales?)\s+(?:a\s+)?consultar/i,
        /consultar\s+(?:por|sobre|si\s+se\s+admiten)?\s*(?:las?\s+)?(?:mascotas?|animales?)/i,
        /(?:segun|según|dependiendo\s+de)\s+(?:la\s+)?mascota/i,
        /(?:solo|sólo|unicamente|únicamente)\s+(?:se\s+)?(?:admiten|aceptan|permiten)\s+(?:mascotas?|animales?)\s+(?:pequenos|pequeños)/i,
        /(?:mascotas?|animales?)\s+(?:pequenos|pequeños)\s+(?:admitid[oa]s?|aceptad[oa]s?)/i,
        /pets?\s+(?:on\s+request|subject\s+to\s+approval)/i
    ];

    var catExceptionPatterns = [
        /(?:excepto|salvo)\s+(?:los\s+)?gatos?/i,
        /gatos?\s+(?:si|sí|admitid[oa]s?|aceptad[oa]s?|permitid[oa]s?)/i
    ];

    var sizeRestrictionPatterns = [
        /no\s+(?:se\s+)?(?:admiten|aceptan|permiten)\s+(?:las?\s+)?(?:mascotas?|animales?)\s+(?:grandes|de\s+gran\s+tamaño)/i
    ];

    var rejectedPatterns = [
        /no\s+(?:se\s+)?(?:admiten|aceptan|permiten|admitimos|aceptamos|permitimos)\s+(?:las?\s+)?(?:mascotas?|animales?)/i,
        /(?:mascotas?|animales?)\s+(?:no\s+)?(?:admitid[oa]s?|aceptad[oa]s?|permitid[oa]s?)\s*:\s*no/i,
        /(?:sin|prohibid[oa]s?)\s+(?:mascotas?|animales?)/i,
        /(?:mascotas?|animales?)\s+no(?:\b|\s)/i,
        /abstenerse\s+(?:personas?\s+)?con\s+(?:mascotas?|animales?)/i,
        /no\s+gatos?(?:\b|\s)/i,
        /(?:no\s+pets?|pets?\s+(?:are\s+)?not\s+allowed)/i
    ];

    function normalize(value) {
        return String(value || "")
            .normalize("NFKC")
            .replace(/[\u2010-\u2015]/g, "-")
            .replace(/\s+/g, " ")
            .trim();
    }

    function findMatches(text, patterns) {
        return patterns.reduce(function (matches, pattern) {
            var match = pattern.exec(text);
            if (match) {
                matches.push({
                    evidence: match[0],
                    index: match.index,
                    pattern: pattern.source
                });
            }
            return matches;
        }, []);
    }

    function classify(text, structuredPetFriendly) {
        var normalized = normalize(text);

        if (structuredPetFriendly === true) {
            return decision(STATUS.ACCEPTED, "structured_field", "pet-friendly field: yes", 1);
        }
        if (structuredPetFriendly === false) {
            return decision(STATUS.REJECTED, "structured_field", "pet-friendly field: no", 1);
        }

        var accepted = findMatches(normalized, acceptedPatterns);
        var conditional = findMatches(normalized, conditionalPatterns);
        var catExceptions = findMatches(normalized, catExceptionPatterns);
        var sizeRestrictions = findMatches(normalized, sizeRestrictionPatterns);
        var rejected = findMatches(normalized, rejectedPatterns);

        if (catExceptions.length > 0) {
            return decision(STATUS.ACCEPTED, "text", catExceptions[0].evidence, 0.99);
        }
        if (sizeRestrictions.length > 0) {
            return decision(STATUS.CONDITIONAL, "text", sizeRestrictions[0].evidence, 0.95);
        }
        if (rejected.length > 0) {
            return decision(STATUS.REJECTED, "text", rejected[0].evidence, 0.99);
        }
        if (conditional.length > 0) {
            return decision(STATUS.CONDITIONAL, "text", conditional[0].evidence, 0.95);
        }
        if (accepted.length > 0) {
            return decision(STATUS.ACCEPTED, "text", accepted[0].evidence, 0.99);
        }

        return decision(
            STATUS.NOT_SPECIFIED,
            "absence",
            "No pet policy found in the available text",
            0.6
        );
    }

    function decision(status, source, evidence, confidence) {
        return {
            status: status,
            source: source,
            evidence: evidence,
            confidence: confidence,
            shouldNotify: status !== STATUS.REJECTED,
            shouldContact: status !== STATUS.REJECTED
        };
    }

    return {
        STATUS: STATUS,
        classify: classify,
        normalize: normalize
    };
})();

if (typeof module !== "undefined" && module.exports) {
    module.exports = PetPolicy;
}
