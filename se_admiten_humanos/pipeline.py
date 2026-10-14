"""End-to-end processing for one normalized rental listing."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Listing, PetDecision
from .persistence import MatchResult, RentalStore, UpsertResult
from .pet_policy import classify
from .search_profile import SearchDecision, SearchProfile, evaluate_listing


@dataclass(frozen=True, slots=True)
class ProfileMatchOutcome:
    """One search profile's independently persisted result."""

    profile: SearchProfile
    match: MatchResult
    decision: SearchDecision
    notification_status: str | None

    @property
    def should_deliver(self) -> bool:
        """Return whether a sender may try to claim this delivery."""

        return self.decision.eligible and self.notification_status in {None, "failed"}


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    """Explain the complete processing result for one listing observation."""

    listing: Listing
    stored_listing: UpsertResult
    pet_decision: PetDecision
    profile_outcomes: tuple[ProfileMatchOutcome, ...]

    @property
    def delivery_candidates(self) -> tuple[ProfileMatchOutcome, ...]:
        """Return eligible matches not already sent or in flight."""

        return tuple(
            outcome for outcome in self.profile_outcomes if outcome.should_deliver
        )


class ListingPipeline:
    """Classify once and match a listing against every enabled search profile."""

    def __init__(self, store: RentalStore, channel: str = "telegram") -> None:
        normalized_channel = channel.strip().casefold()
        if not normalized_channel:
            raise ValueError("Notification channel cannot be empty")
        self.store = store
        self.channel = normalized_channel

    def process(
        self,
        listing: Listing,
        structured_pet_friendly: bool | None = None,
    ) -> ProcessingResult:
        """Persist, classify, evaluate, and expose safe delivery candidates."""

        policy_text = "\n".join(
            value for value in (listing.title, listing.description) if value
        )
        pet_decision = classify(policy_text, structured_pet_friendly)
        stored_listing = self.store.upsert(listing)
        outcomes: list[ProfileMatchOutcome] = []

        for profile in self.store.load_enabled_search_profiles():
            decision = evaluate_listing(listing, pet_decision, profile)
            match = self.store.record_match(
                stored_listing.listing_id,
                profile.id,
                decision,
                pet_decision,
            )
            outcomes.append(
                ProfileMatchOutcome(
                    profile=profile,
                    match=match,
                    decision=decision,
                    notification_status=self.store.notification_status(
                        match.match_id,
                        self.channel,
                    ),
                )
            )

        return ProcessingResult(
            listing=listing,
            stored_listing=stored_listing,
            pet_decision=pet_decision,
            profile_outcomes=tuple(outcomes),
        )
