import Combine
import Foundation

/// Central Combine-based event bus.
/// Producers call `emit(_:)`. Consumers subscribe via `publisher`.
/// Neither side knows about the other.
final class EventBus {
    static let shared = EventBus()
    private init() {}

    private let subject = PassthroughSubject<AppEvent, Never>()

    var publisher: AnyPublisher<AppEvent, Never> {
        subject.eraseToAnyPublisher()
    }

    func emit(_ event: AppEvent) {
        subject.send(event)
    }
}
