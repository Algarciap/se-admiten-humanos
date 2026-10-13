"use strict";

var OutreachMessage = (function () {
    function listingReference(listing) {
        var parts = [];
        if (listing.title) {
            parts.push(listing.title);
        }
        if (listing.price) {
            parts.push(listing.price);
        }
        if (listing.location) {
            parts.push(listing.location);
        }
        return parts.length > 0 ? parts.join(" · ") : "la vivienda anunciada";
    }

    function renterSummary(profile) {
        return (
            profile.renterName +
            " tiene contrato indefinido como " +
            profile.job +
            ", con ingresos mensuales de " +
            profile.monthlyIncome +
            " €, y viviría con " +
            profile.pets +
            "."
        );
    }

    function build(listing, petDecision, profile) {
        var reference = listingReference(listing);
        var greeting = listing.contactName ? "Hola, " + listing.contactName + ":" : "Hola:";
        var opening =
            "Mi nombre es " +
            profile.senderName +
            " y les escribo en nombre de " +
            profile.renterName +
            ", que está interesada en " +
            reference +
            " y busca mudarse lo antes posible.";
        var question;

        if (petDecision.status === "accepted") {
            question =
                "Hemos visto que la vivienda admite mascotas. ¿Sería posible concertar una visita lo antes posible?";
        } else {
            question =
                "¿Podrían confirmarnos si se admiten " +
                profile.pets +
                " y si sería posible concertar una visita?";
        }

        return [
            greeting,
            "",
            opening,
            "",
            renterSummary(profile),
            "",
            question,
            "",
            "Muchas gracias. Quedo pendiente de su respuesta.",
            "",
            "Un saludo,",
            profile.senderName
        ].join("\n");
    }

    function subject(listing) {
        return "Interés en alquiler: " + listingReference(listing);
    }

    return {
        build: build,
        subject: subject
    };
})();

if (typeof module !== "undefined" && module.exports) {
    module.exports = OutreachMessage;
}
