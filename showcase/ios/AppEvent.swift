import CoreLocation
import Foundation

/// All first-party events that flow through the app's EventBus.
/// Phase 1 events are fully wired. Phase 3 events (WebSocket, APNs) are
/// defined here to lock the contract early — payloads will be added when
/// those phases are implemented.
enum AppEvent {
    // MARK: - Location (Phase 1)
    case locationUpdated(CLLocation)
    case locationPermissionChanged(CLAuthorizationStatus)

    // MARK: - Filters / Search (Phase 1)
    case filtersChanged(FilterOptions)
    case searchQueryChanged(String)

    // MARK: - App Lifecycle (Phase 1)
    case appForegrounded
    case appBackgrounded

    // MARK: - Server Push (Phase 3 — WebSocket)
    case happyHourChanged(String)       // restaurantId
    case serverDataRefreshed            // generic refresh signal

    // MARK: - Notifications (Phase 3 — APNs)
    case notificationPermissionGranted
    case deviceTokenRegistered(String)  // APNs device token
}
