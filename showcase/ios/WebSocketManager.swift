import Combine
import Foundation

// MARK: - Server event types

private struct ServerEvent: Decodable {
    let eventType: String
    let eventId: String
    let correlationId: String
    let payload: ServerEventPayload
}

private struct ServerEventPayload: Decodable {
    let restaurantId: String?
    let diffSummary: [String]?
}

// MARK: - WebSocketManager

/// Maintains a persistent WebSocket connection to the backend and routes
/// incoming server events into the EventBus.
///
/// Lifetime: held exclusively as @StateObject in HappyHourTrackerApp.
/// No view holds a direct reference — all communication is via EventBus.
@MainActor
final class WebSocketManager: ObservableObject {

    // MARK: URL

    private static let wsURL: URL = {
        #if DEBUG
        return URL(string: "ws://localhost:8000/ws")!
        #else
        return URL(string: "wss://api.yourdomain.com/ws")!
        #endif
    }()

    // MARK: State

    private var task: URLSessionWebSocketTask?
    private var reconnectDelay: TimeInterval = 1.0
    private let maxReconnectDelay: TimeInterval = 30.0
    private var cancellables = Set<AnyCancellable>()

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    // MARK: Init

    init() {
        EventBus.shared.publisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] event in
                switch event {
                case .appForegrounded:
                    // AppLifecycleMonitor → EventBus → here → reconnect
                    self?.connect()
                case .appBackgrounded:
                    self?.disconnect()
                default:
                    break
                }
            }
            .store(in: &cancellables)

        connect()
    }

    // MARK: Connect / Disconnect

    func connect() {
        // Cancel any existing task before opening a new one (idempotent)
        task?.cancel(with: .goingAway, reason: nil)
        task = nil

        let wsTask = URLSession.shared.webSocketTask(with: Self.wsURL)
        task = wsTask
        wsTask.resume()
        reconnectDelay = 1.0
        receiveMessage()
    }

    private func disconnect() {
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        // No reconnect scheduled — disconnection was intentional (app backgrounded)
    }

    // MARK: Receive loop

    private func receiveMessage() {
        guard let task else { return }

        task.receive { [weak self] result in
            guard let self else { return }
            Task { @MainActor in
                switch result {
                case .success(let message):
                    self.handleMessage(message)
                    self.receiveMessage()   // reschedule — keep the loop alive
                case .failure:
                    self.scheduleReconnect()
                }
            }
        }
    }

    private func handleMessage(_ message: URLSessionWebSocketTask.Message) {
        let data: Data
        switch message {
        case .data(let d):
            data = d
        case .string(let s):
            guard let d = s.data(using: .utf8) else { return }
            data = d
        @unknown default:
            return
        }

        guard let event = try? decoder.decode(ServerEvent.self, from: data) else { return }

        switch event.eventType {
        case "happy_hour_changed":
            if let restaurantId = event.payload.restaurantId {
                EventBus.shared.emit(.happyHourChanged(restaurantId))
            }
        case "restaurant_added":
            EventBus.shared.emit(.serverDataRefreshed)
        default:
            break
        }
    }

    // MARK: Reconnect

    private func scheduleReconnect() {
        let delay = reconnectDelay
        reconnectDelay = min(reconnectDelay * 2, maxReconnectDelay)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.connect()
        }
    }
}
