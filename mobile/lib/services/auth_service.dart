import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:local_auth/local_auth.dart';
import 'api_client.dart';

class User {
  final int id;
  final String email;
  final String role;
  final int? clientId;

  User({
    required this.id,
    required this.email,
    required this.role,
    this.clientId,
  });

  factory User.fromJson(Map<String, dynamic> json) {
    return User(
      id: json['id'],
      email: json['email'],
      role: json['role'],
      clientId: json['client_id'],
   );
  }
}

class AuthService {
  final ApiClient _api;
  final FlutterSecureStorage _storage = const FlutterSecureStorage();
  final LocalAuthentication _localAuth = LocalAuthentication();

  AuthService(this._api);

  Future<User?> login(String email, String password) async {
    final response = await _api.login(email, password);
    if (response != null) {
      await _storage.write(key: 'token', value: response['access_token']);
      return getCurrentUser();
    }
    return null;
  }

  Future<User?> getCurrentUser() async {
    final token = await _storage.read(key: 'token');
    if (token == null) return null;

    try {
      final response = await _api.get('/api/auth/me');
      if (response != null) {
        return User.fromJson(response);
      }
    } catch (e) {
      await logout();
    }
    return null;
  }

  Future<void> logout() async {
    await _storage.delete(key: 'token');
  }

  Future<bool> canCheckBiometrics() async {
    try {
      return await _localAuth.canCheckBiometrics;
    } catch (_) {
      return false;
    }
  }

  Future<bool> authenticateWithBiometrics() async {
    try {
      return await _localAuth.authenticate(
        localizedReason: 'Authenticate to access your trading account',
        options: const AuthenticationOptions(
          stickyAuth: true,
          biometricOnly: true,
        ),
      );
    } catch (_) {
      return false;
    }
  }
}

final authProvider = StateNotifierProvider<AuthNotifier, AsyncValue<User?>>((ref) {
  return AuthNotifier(ref.read(apiProvider));
});

class AuthNotifier extends StateNotifier<AsyncValue<User?>> {
  final ApiClient _api;

  AuthNotifier(this._api) : super(const AsyncValue.loading()) {
    _loadUser();
  }

  Future<void> _loadUser() async {
    try {
      final authService = AuthService(_api);
      final user = await authService.getCurrentUser();
      state = AsyncValue.data(user);
    } catch (e) {
      state = const AsyncValue.data(null);
    }
  }

  Future<void> login(String email, String password) async {
    state = const AsyncValue.loading();
    try {
      final authService = AuthService(_api);
      final user = await authService.login(email, password);
      state = AsyncValue.data(user);
    } catch (e) {
      state = AsyncValue.error(e, StackTrace.current);
    }
  }

  Future<void> logout() async {
    final authService = AuthService(_api);
    await authService.logout();
    state = const AsyncValue.data(null);
  }
}
