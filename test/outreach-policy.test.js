"use strict";

var test = require("node:test");
var assert = require("node:assert/strict");
var policy = require("../src/apps-script-main.js");

function propertiesWithAllowlist(value) {
    return {
        getProperty: function (name) {
            return name === "AUTO_EMAIL_DOMAIN_ALLOWLIST" ? value : "";
        }
    };
}

test("contact extraction fails closed without an allowlist", function () {
    var text = "Contacto: agente@agencia.example";
    assert.equal(policy.approvedContactEmail(text, propertiesWithAllowlist("")), "");
});

test("contact extraction accepts only an explicitly allowed domain", function () {
    var text = "Avisos: noreply@portal.example. Contacto: agente@agencia.example";
    var properties = propertiesWithAllowlist("agencia.example");
    assert.equal(policy.approvedContactEmail(text, properties), "agente@agencia.example");
});

test("a deceptive lookalike domain is rejected", function () {
    var properties = propertiesWithAllowlist("agencia.example");
    assert.equal(policy.emailDomainAllowed("agente@agencia.example.evil.test", properties), false);
});
