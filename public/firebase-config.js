/**
 * Firebase Configuration for Nhay Cloud
 * Replace the placeholder values with your actual Firebase project config.
 */
export const firebaseConfig = {
  apiKey: "AIzaSyCpyI7cWuFp5y_LIUhxv7inWR-pk6Wqem0",
  authDomain: "wallpaper-6cbbe.firebaseapp.com",
  projectId: "wallpaper-6cbbe",
  storageBucket: "wallpaper-6cbbe.firebasestorage.app",
  messagingSenderId: "1018323809946",
  appId: "1:1018323809946:web:fc1444f496ef22867b9123",
  measurementId: "G-T1N24XCQEN"
};

/**
 * Admin emails authorized to access the management panel.
 */
export const ADMIN_EMAILS = ["traderfinn0312@gmail.com", "dinhhoangvan.hh@gmail.com"];

/**
 * FIRESTORE SECURITY RULES (Example)
 * Copy and paste these into your Firebase Console -> Firestore -> Rules
 *
 * rules_version = '2';
 * service cloud.firestore {
 *   match /databases/{database}/documents {
 *     // Function to check if user is admin
 *     function isAdmin() {
 *       return request.auth != null && request.auth.token.email in ["your-email@gmail.com"];
 *     }
 *
 *     // Users can read/write their own profile (but not direct coin updates)
 *     match /users/{userId} {
 *       allow read: if request.auth != null && request.auth.uid == userId;
 *       allow create: if request.auth != null && request.auth.uid == userId;
 *       allow update: if isAdmin(); // Only admin can update coins/profile
 *     }
 *
 *     // Orders
 *     match /orders/{orderId} {
 *       allow read: if request.auth != null && (resource.data.userId == request.auth.uid || isAdmin());
 *       allow create: if request.auth != null;
 *       allow update: if isAdmin();
 *     }
 *
 *     // Topup requests
 *     match /topups/{topupId} {
 *       allow read: if request.auth != null && (resource.data.userId == request.auth.uid || isAdmin());
 *       allow create: if request.auth != null;
 *       allow update: if isAdmin();
 *     }
 *
 *     // Bots (bật/tắt bot + engine render — Admin UI ghi activeRenderProvider)
 *     match /bots/{botId} {
 *       allow read: if request.auth != null && isAdmin();
 *       allow write: if isAdmin();
 *     }
 *
 *     // Xây kênh tự động (batch channel)
 *     match /batchChannelConfig/{docId} {
 *       allow read, write: if isAdmin();
 *     }
 *     match /batchChannelRuns/{runId} {
 *       allow read: if isAdmin();
 *       allow create, update, delete: if false;
 *     }
 *
 *     // (Tuỳ chọn) settings/render — UI hiện dùng bots/nhaycloud_vps_bot thay vì doc này
 *     match /settings/{docId} {
 *       allow read: if request.auth != null && isAdmin();
 *       allow write: if isAdmin();
 *     }
 *   }
 * }
 */
