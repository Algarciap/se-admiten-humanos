"use strict";

var fs = require("node:fs");
var path = require("node:path");

var file = path.join(__dirname, "..", "data", "sources.json");
var sources = JSON.parse(fs.readFileSync(file, "utf8"));
var required = [
    "id",
    "name",
    "kind",
    "priority",
    "base_url",
    "acquisition",
    "pet_signal",
    "verified_on"
];
var ids = new Set();
var errors = [];

sources.forEach(function (source, index) {
    required.forEach(function (field) {
        if (source[field] === undefined || source[field] === "") {
            errors.push("source[" + index + "] is missing " + field);
        }
    });
    if (ids.has(source.id)) {
        errors.push("duplicate source id: " + source.id);
    }
    ids.add(source.id);
    try {
        new URL(source.base_url);
    } catch (error) {
        errors.push("invalid base_url for " + source.id);
    }
});

if (errors.length > 0) {
    console.error(errors.join("\n"));
    process.exitCode = 1;
} else {
    console.log("Validated " + sources.length + " sources.");
}
