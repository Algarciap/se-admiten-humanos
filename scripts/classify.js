"use strict";

var fs = require("node:fs");
var PetPolicy = require("../src/pet-policy.js");

var text = fs.readFileSync(0, "utf8");
process.stdout.write(JSON.stringify(PetPolicy.classify(text), null, 2) + "\n");
