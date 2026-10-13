"use strict";

var INBOX_LABEL = "rent-alerts";
var PROCESSED_LABEL = "rent-alerts/processed";
var ERROR_LABEL = "rent-alerts/error";

function installRentAlertTrigger() {
    ensureLabel_(INBOX_LABEL);
    ensureLabel_(PROCESSED_LABEL);
    ensureLabel_(ERROR_LABEL);

    ScriptApp.getProjectTriggers()
        .filter(function (trigger) {
            return trigger.getHandlerFunction() === "processRentAlerts";
        })
        .forEach(function (trigger) {
            ScriptApp.deleteTrigger(trigger);
        });

    ScriptApp.newTrigger("processRentAlerts").timeBased().everyMinutes(1).create();
}

function processRentAlerts() {
    var lock = LockService.getScriptLock();
    if (!lock.tryLock(1000)) {
        return;
    }

    try {
        var threads = GmailApp.search(
            "label:" + INBOX_LABEL + " -label:" + PROCESSED_LABEL + " newer_than:7d",
            0,
            25
        );
        var processedLabel = ensureLabel_(PROCESSED_LABEL);
        var errorLabel = ensureLabel_(ERROR_LABEL);

        threads.forEach(function (thread) {
            try {
                processThread_(thread);
                thread.addLabel(processedLabel);
                thread.removeLabel(errorLabel);
            } catch (error) {
                thread.addLabel(errorLabel);
                console.error(error && error.stack ? error.stack : error);
            }
        });
    } finally {
        lock.releaseLock();
    }
}

function processThread_(thread) {
    var messages = thread.getMessages();
    var message = messages[messages.length - 1];
    var subject = message.getSubject();
    var plainText = message.getPlainBody();
    var html = message.getBody();
    var properties = PropertiesService.getScriptProperties();
    var sourceId = SourceRegistry.sourceIdFromText(message.getFrom() + " " + html);
    var listing = extractListing_(subject, plainText, html, sourceId, properties);
    var decision = PetPolicy.classify(plainText, listing.structuredPetFriendly);

    if (!decision.shouldNotify || alreadySeen_(listing)) {
        return;
    }

    sendTelegram_(listing, decision);
    prepareOutreach_(listing, decision);
    remember_(listing);
}

function extractListing_(subject, plainText, html, sourceId, properties) {
    var urls = [];
    var hrefPattern = /href=["']([^"']+)["']/gi;
    var match;
    while ((match = hrefPattern.exec(html)) !== null) {
        var candidate = decodeHtml_(match[1]);
        if (SourceRegistry.allowedListingUrl(candidate) && urls.indexOf(candidate) === -1) {
            urls.push(candidate);
        }
    }

    var priceMatch = plainText.match(/\b([0-9]{3,5}(?:[.,][0-9]{1,2})?)\s*€(?:\s*\/\s*mes)?/i);

    return {
        sourceId: sourceId,
        title: subject,
        price: priceMatch ? priceMatch[1] + " €" : "",
        location: "",
        url: urls[0] || "",
        contactEmail: approvedContactEmail_(plainText, properties),
        structuredPetFriendly: null
    };
}

function prepareOutreach_(listing, decision) {
    var properties = PropertiesService.getScriptProperties();
    var profile = {
        senderName: properties.getProperty("OUTREACH_FROM_NAME"),
        renterName: properties.getProperty("RENTER_NAME"),
        monthlyIncome: properties.getProperty("RENTER_MONTHLY_INCOME"),
        job: properties.getProperty("RENTER_JOB"),
        pets: properties.getProperty("RENTER_PETS")
    };

    if (!listing.contactEmail || Object.keys(profile).some(function (key) {
        return !profile[key];
    })) {
        return;
    }

    var body = OutreachMessage.build(listing, decision, profile);
    var subject = OutreachMessage.subject(listing);
    var mode = properties.getProperty("OUTREACH_MODE") || "draft";

    if (mode === "auto_email" && emailDomainAllowed_(listing.contactEmail, properties)) {
        GmailApp.sendEmail(listing.contactEmail, subject, body);
        return;
    }

    GmailApp.createDraft(listing.contactEmail, subject, body);
}

function emailDomainAllowed_(email, properties) {
    var domain = email.split("@").pop().toLowerCase();
    var allowlist = (properties.getProperty("AUTO_EMAIL_DOMAIN_ALLOWLIST") || "")
        .split(",")
        .map(function (value) {
            return value.trim().toLowerCase();
        })
        .filter(Boolean);
    return allowlist.indexOf(domain) !== -1;
}

function approvedContactEmail_(text, properties) {
    var matches = String(text || "").match(
        /\b[A-Z0-9._%+-]+@(?:[A-Z0-9-]+\.)+[A-Z]{2,}\b/gi
    ) || [];

    for (var i = 0; i < matches.length; i += 1) {
        var candidate = matches[i].toLowerCase();
        if (emailDomainAllowed_(candidate, properties)) {
            return candidate;
        }
    }
    return "";
}

function sendTelegram_(listing, decision) {
    var properties = PropertiesService.getScriptProperties();
    var token = properties.getProperty("TELEGRAM_BOT_TOKEN");
    var chatId = properties.getProperty("TELEGRAM_CHAT_ID");

    if (!token || !chatId) {
        throw new Error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required");
    }

    var icons = {
        accepted: "✅",
        conditional: "🟡",
        not_specified: "⚪"
    };
    var lines = [
        (icons[decision.status] || "🏠") + " " + decision.status,
        listing.title,
        [listing.price, listing.location, listing.sourceId].filter(Boolean).join(" · "),
        "Evidence: " + decision.evidence,
        listing.url
    ].filter(Boolean);

    var response = UrlFetchApp.fetch("https://api.telegram.org/bot" + token + "/sendMessage", {
        method: "post",
        contentType: "application/json",
        muteHttpExceptions: true,
        payload: JSON.stringify({
            chat_id: chatId,
            text: lines.join("\n"),
            disable_web_page_preview: false
        })
    });

    if (response.getResponseCode() < 200 || response.getResponseCode() >= 300) {
        throw new Error("Telegram delivery failed: HTTP " + response.getResponseCode());
    }
}

function alreadySeen_(listing) {
    return Boolean(PropertiesService.getScriptProperties().getProperty(dedupeKey_(listing)));
}

function remember_(listing) {
    PropertiesService.getScriptProperties().setProperty(
        dedupeKey_(listing),
        new Date().toISOString()
    );
}

function dedupeKey_(listing) {
    var identity = [listing.sourceId, listing.url, listing.title, listing.price].join("|");
    var digest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, identity);
    return "seen_" + Utilities.base64EncodeWebSafe(digest).replace(/=+$/, "");
}

function ensureLabel_(name) {
    return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name);
}

function decodeHtml_(value) {
    return String(value)
        .replace(/&amp;/g, "&")
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'");
}

if (typeof module !== "undefined" && module.exports) {
    module.exports = {
        approvedContactEmail: approvedContactEmail_,
        emailDomainAllowed: emailDomainAllowed_
    };
}
