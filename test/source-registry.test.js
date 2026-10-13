"use strict";

var test = require("node:test");
var assert = require("node:assert/strict");
var SourceRegistry = require("../src/source-registry.js");

test("recognizes supported source domains", function () {
    assert.equal(
        SourceRegistry.sourceIdFromText("alerta@idealista.com https://www.idealista.com/inmueble/1"),
        "idealista"
    );
    assert.equal(
        SourceRegistry.sourceIdFromText("https://www.vivenio.com/edificio/example"),
        "vivenio"
    );
});

test("allows only registered listing domains", function () {
    assert.equal(SourceRegistry.allowedListingUrl("https://www.testahomes.com/viviendas/example"), true);
    assert.equal(SourceRegistry.allowedListingUrl("https://evil.example/idealista.com"), false);
    assert.equal(SourceRegistry.allowedListingUrl("not a url"), false);
});
