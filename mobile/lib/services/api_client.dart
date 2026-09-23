import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

class ApiClient {
  late final Dio _dio;
  final FlutterSecureStorage _storage = const FlutterSecureStorage();
  final String baseUrl;

  ApiClient({this.baseUrl = 'http://localhost:8000'}) {
    _dio = Dio(BaseOptions(
      baseUrl: baseUrl,
      connectTimeout: const Duration(seconds: 10),
      receiveTimeout: const Duration(seconds: 10),
      headers: {'Content-Type': 'application/json'},
    ));

    _dio.interceptors.add(InterceptorsWrapper(
      onRequest: (options, handler) async {
        final token = await _storage.read(key: 'token');
        if (token != null) {
          options.headers['Authorization'] = 'Bearer $token';
        }
        handler.next(options);
      },
      onError: (error, handler) async {
        if (error.response?.statusCode == 401) {
          await _storage.delete(key: 'token');
        }
        handler.next(error);
      },
    ));
  }

  Future<Map<String, dynamic>?> login(String email, String password) async {
    try {
      final response = await _dio.post('/api/auth/login', data: {
        'username': email,
        'password': password,
      });
      return response.data;
    } on DioException {
      return null;
    }
  }

  Future<Map<String, dynamic>?> get(String path) async {
    try {
      final response = await _dio.get(path);
      return response.data;
    } on DioException {
      return null;
    }
  }

  Future<List<dynamic>?> getList(String path) async {
    try {
      final response = await _dio.get(path);
      return response.data as List<dynamic>;
    } on DioException {
      return null;
    }
  }
}

final apiProvider = Provider<ApiClient>((ref) {
  return ApiClient();
});
