import Foundation

struct HappyHour: Codable, Identifiable {
    let id: String
    let restaurantId: String
    let source: String
    let days: [String]
    let startTime: String?
    let endTime: String?
    let deals: String?
    let structuredDeals: [StructuredDeal]?
    let confidenceScore: Double
    let isCurrent: Bool
    let scrapedAt: Date
    let createdAt: Date
}

struct StructuredDeal: Codable {
    let item: String
    let category: String
    let dealPrice: Double?
    let originalPrice: Double?
    let discountPct: Double?
    let description: String?
}
