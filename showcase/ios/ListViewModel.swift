import Combine
import SwiftUI

@MainActor
class ListViewModel: ObservableObject {
    @Published var restaurants: [Restaurant] = []
    @Published var isLoading = false
    @Published var errorMessage: String?
    @Published var filters = FilterOptions()
    @Published var showActiveNowOnly = false

    private let apiClient: APIClientProtocol
    private var cancellables = Set<AnyCancellable>()
    private var lastFetchedAt: Date?

    init(apiClient: APIClientProtocol = APIClient.shared) {
        self.apiClient = apiClient

        EventBus.shared.publisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] event in
                guard let self else { return }
                switch event {
                case .filtersChanged(let options):
                    self.filters = options
                    Task { await self.loadRestaurants() }
                case .appForegrounded:
                    guard self.isStale else { return }
                    Task { await self.loadRestaurants() }
                default:
                    break
                }
            }
            .store(in: &cancellables)
    }

    private var isStale: Bool {
        guard let lastFetchedAt else { return true }
        return Date().timeIntervalSince(lastFetchedAt) > 300
    }

    func loadRestaurants() async {
        isLoading = true
        errorMessage = nil
        do {
            if showActiveNowOnly {
                let happyHours: [HappyHour] = try await apiClient.request(
                    .activeHappyHours(latitude: nil, longitude: nil)
                )
                let activeIds = Set(happyHours.map { $0.restaurantId })
                let all: [Restaurant] = try await apiClient.request(
                    .restaurants(city: filters.city, cuisineType: filters.cuisineType)
                )
                restaurants = all.filter { activeIds.contains($0.id) }
            } else {
                let results: [Restaurant] = try await apiClient.request(
                    .restaurants(city: filters.city, cuisineType: filters.cuisineType)
                )
                restaurants = results
            }
            lastFetchedAt = Date()
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }
}
