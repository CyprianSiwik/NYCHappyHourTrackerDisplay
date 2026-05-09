import Foundation

protocol APIClientProtocol {
    func request<T: Decodable>(_ endpoint: Endpoint) async throws -> T
}

extension APIClient: APIClientProtocol {}
