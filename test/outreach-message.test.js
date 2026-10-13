"use strict";

var test = require("node:test");
var assert = require("node:assert/strict");
var OutreachMessage = require("../src/outreach-message.js");

var profile = {
    senderName: "Persona remitente",
    renterName: "Persona interesada",
    monthlyIncome: "2500",
    job: "profesional de producción",
    pets: "dos gatos"
};

var listing = {
    title: "Piso en Chamberí",
    price: "1.250 €",
    location: "Madrid"
};

test("unknown policy asks explicitly about two cats", function () {
    var message = OutreachMessage.build(listing, { status: "not_specified" }, profile);
    assert.match(message, /dos gatos/);
    assert.match(message, /confirmarnos si se admiten dos gatos/);
    assert.match(message, /Persona remitente/);
});

test("accepted policy asks for a visit", function () {
    var message = OutreachMessage.build(listing, { status: "accepted" }, profile);
    assert.match(message, /Hemos visto que la vivienda admite mascotas/);
    assert.match(message, /concertar una visita/);
});
