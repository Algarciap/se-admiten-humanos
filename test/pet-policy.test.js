"use strict";

var test = require("node:test");
var assert = require("node:assert/strict");
var PetPolicy = require("../src/pet-policy.js");

var cases = [
    ["No se admiten animales.", "rejected"],
    ["No aceptamos mascotas", "rejected"],
    ["Se alquila sin mascotas", "rejected"],
    ["Mascotas no", "rejected"],
    ["No pets allowed", "rejected"],
    ["Se aceptan mascotas", "accepted"],
    ["Pet friendly", "accepted"],
    ["No hay problema con las mascotas", "accepted"],
    ["Gatos permitidos", "accepted"],
    ["Mascotas a consultar", "conditional"],
    ["Solo se admiten animales pequeños", "conditional"],
    ["No se admiten mascotas de gran tamaño", "conditional"],
    ["No se admiten animales excepto gatos", "accepted"],
    ["No perros, gatos sí", "accepted"],
    ["No perros", "not_specified"],
    ["No gatos", "rejected"],
    ["Piso luminoso junto al metro", "not_specified"]
];

cases.forEach(function (entry) {
    test(entry[0] + " -> " + entry[1], function () {
        assert.equal(PetPolicy.classify(entry[0]).status, entry[1]);
    });
});

test("a structured field takes precedence", function () {
    assert.equal(PetPolicy.classify("", true).status, "accepted");
    assert.equal(PetPolicy.classify("Se aceptan mascotas", false).status, "rejected");
});
