# GitHub Branch Protection Ayarları / GitHub Branch Protection Settings

## Türkçe

### Adım 1: Branch Protection Rule Ekleme

1. GitHub'da repo'nuza gidin
2. **Settings** → **Branches** tıklayın
3. **Add rule** butonuna tıklayın
4. **Branch name pattern**: `main` yazın (veya `master`)

### Adım 2: Koruma Ayarları

Aşağıdaki seçenekleri işaretleyin:

#### Pull Request Gereksinimleri
- ✅ **Require a pull request before merging**
- ✅ **Require approvals** → **Required number of approvals before merging: 1**
- ✅ **Dismiss stale PR approvals when new commits are pushed**
- ✅ **Require review from CODEOWNERS**

#### Durum Kontrolleri
- ✅ **Require status checks to pass before merging**
- Status checks: `PR Checks` seçin

#### Push Kısıtlamaları
- ✅ **Restrict pushes that create files**
- ✅ **Require linear history**
- ✅ **Include administrators** (Admin bile olsanız kurallara uymanız gerekir)

### Adım 3: Collaborator Yetkileri

1. **Settings** → **Collaborators and teams**
2. Kişileri eklerken **Permission** olarak seçin:
   - **Read**: Sadece okuma (önerilen)
   - **Triage**: Issue/PR yönetebilir (önerilen)
   - **Write**: Direkt commit atamaz (PR gerekir)

### Adım 4: CODEOWNERS Aktifleştirme

`.github/CODEOWNERS` dosyası zaten oluşturuldu. GitHub otomatik olarak bunu kullanacaktır.

---

## English

### Step 1: Add Branch Protection Rule

1. Go to your GitHub repository
2. Click **Settings** → **Branches**
3. Click **Add rule** button
4. Enter **Branch name pattern**: `main` (or `master`)

### Step 2: Protection Settings

Check the following options:

#### Pull Request Requirements
- ✅ **Require a pull request before merging**
- ✅ **Require approvals** → **Required number of approvals before merging: 1**
- ✅ **Dismiss stale PR approvals when new commits are pushed**
- ✅ **Require review from CODEOWNERS**

#### Status Checks
- ✅ **Require status checks to pass before merging**
- Select status checks: `PR Checks`

#### Push Restrictions
- ✅ **Restrict pushes that create files**
- ✅ **Require linear history**
- ✅ **Include administrators** (Admins must also follow rules)

### Step 3: Collaborator Permissions

1. Go to **Settings** → **Collaborators and teams**
2. When adding people, select **Permission**:
   - **Read**: Read-only (recommended)
   - **Triage**: Can manage issues/PRs (recommended)
   - **Write**: Cannot push directly (PR required)

### Step 4: Enable CODEOWNERS

The `.github/CODEOWNERS` file is already created. GitHub will automatically use it.

---

## Sonuç / Result

Bu ayarlarla:
- ✅ Kimse doğrudan `main` branch'e push yapamaz
- ✅ Tüm değişiklikler PR üzerinden gelmeli
- ✅ Tüm PR'lar sizin (@senoldogan) onayınıza gerek duyar
- ✅ Testler geçmeden merge yapılamaz

With these settings:
- ✅ No one can push directly to `main` branch
- ✅ All changes must come via Pull Request
- ✅ All PRs require approval from you (@senoldogan)
- ✅ Cannot merge if tests fail
