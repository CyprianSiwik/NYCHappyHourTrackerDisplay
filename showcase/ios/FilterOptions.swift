import Foundation

struct FilterOptions {
    var city: String?
    var cuisineType: String?
    var vibeTag: String?
    var day: String?
    var minConfidence: Double = 0.0
    var radiusMiles: Double = 5.0
}
