import Foundation

struct Restaurant: Codable, Identifiable, Hashable {
    let id: String
    let name: String
    let address: String
    let city: String
    let state: String
    let zipCode: String
    let latitude: Double
    let longitude: Double
    let phone: String?
    let website: String?
    let yelpUrl: String?
    let instagramHandle: String?
    let cuisineType: String?
    let vibeTags: [String]
    let createdAt: Date
    let updatedAt: Date
}
