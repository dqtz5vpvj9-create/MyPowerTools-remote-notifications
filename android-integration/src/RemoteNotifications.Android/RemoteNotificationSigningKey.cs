using System.Text;
using MyPowerTools.Platform.Abstractions;
using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Signers;
using Org.BouncyCastle.Crypto.Utilities;
using Org.BouncyCastle.Utilities.IO.Pem;

namespace RemoteNotifications.Android;

/// <summary>
/// Platform credential-store backed signing identity for the signed <c>/pull</c> handshake.
///
/// The desktop product signs the fixed text <c>hello</c> with an unencrypted OpenSSH Ed25519 key that
/// lives in <c>~/.ssh</c>. Android has no such file: the key is imported once by the user, stored in
/// the platform <see cref="ISecretStore"/> (Android Keystore on device) and read back only to produce
/// the signature. The deterministic signature is cached — never the key material — and a monotonic
/// generation counter invalidates the cache on import/clear, mirroring the desktop signer's
/// "cache the deterministic result, not private key material" contract without adding a key hash.
/// </summary>
internal sealed class RemoteNotificationSigningKey
{
    private static readonly byte[] Handshake = Encoding.ASCII.GetBytes("hello");

    private readonly ISecretStore _secrets;
    private readonly string _moduleId;
    private readonly object _gate = new();
    private string? _cachedSignature;
    private long _generation;
    private long _cachedGeneration = -1;

    public RemoteNotificationSigningKey(ISecretStore secrets, string moduleId)
    {
        _secrets = secrets ?? throw new ArgumentNullException(nameof(secrets));
        ArgumentException.ThrowIfNullOrWhiteSpace(moduleId);
        _moduleId = moduleId;
    }

    public SecretReference Reference =>
        SecretReference.Create(_moduleId, RemoteNotificationsAndroidOptions.SigningKeySecretName);

    public async Task<bool> IsConfiguredAsync(CancellationToken cancellationToken)
    {
        return await ReadAsync(cancellationToken).ConfigureAwait(false) is not null;
    }

    /// <summary>Returns the base64url Ed25519 signature of <c>hello</c>, or throws when no key is imported.</summary>
    public async Task<string> GetHandshakeSignatureAsync(CancellationToken cancellationToken)
    {
        lock (_gate)
        {
            if (_cachedSignature is not null && _cachedGeneration == _generation)
            {
                return _cachedSignature;
            }
        }

        var pem = await ReadAsync(cancellationToken).ConfigureAwait(false)
            ?? throw new InvalidOperationException(
                "尚未导入签名密钥。请在“远程通知”页面导入与桌面端相同的 OpenSSH Ed25519 私钥。");
        var signature = SignHandshakePem(pem);
        lock (_gate)
        {
            _cachedSignature = signature;
            _cachedGeneration = _generation;
        }

        return signature;
    }

    public async Task SaveAsync(string openSshPrivateKey, CancellationToken cancellationToken)
    {
        ValidatePem(openSshPrivateKey);
        await _secrets.SaveAsync(_moduleId, RemoteNotificationsAndroidOptions.SigningKeySecretName, openSshPrivateKey, cancellationToken)
            .ConfigureAwait(false);
        Invalidate();
    }

    public async Task ClearAsync(CancellationToken cancellationToken)
    {
        await _secrets.DeleteAsync(Reference, cancellationToken).ConfigureAwait(false);
        Invalidate();
    }

    private void Invalidate()
    {
        lock (_gate)
        {
            _cachedSignature = null;
            _cachedGeneration = -1;
            _generation++;
        }
    }

    private async Task<string?> ReadAsync(CancellationToken cancellationToken)
    {
        var value = await _secrets.ReadAsync(Reference, cancellationToken).ConfigureAwait(false);
        return string.IsNullOrWhiteSpace(value) ? null : value;
    }

    /// <summary>Signs <c>hello</c> exactly like the desktop signer, so one server key authorizes both clients.</summary>
    internal static string SignHandshakePem(string openSshPrivateKey)
    {
        var privateKey = ParsePrivateKey(openSshPrivateKey);
        var signer = new Ed25519Signer();
        signer.Init(forSigning: true, privateKey);
        signer.BlockUpdate(Handshake, 0, Handshake.Length);
        var signature = signer.GenerateSignature();
        return Convert.ToBase64String(signature).Replace('+', '-').Replace('/', '_');
    }

    /// <summary>
    /// Accepts only an unencrypted OpenSSH Ed25519 private key. The message never contains key
    /// material: failures are reported by shape, not by echoing the offending text.
    /// </summary>
    internal static void ValidatePem(string openSshPrivateKey)
    {
        if (string.IsNullOrWhiteSpace(openSshPrivateKey))
        {
            throw new InvalidDataException("签名密钥内容为空。");
        }

        var privateKey = ParsePrivateKey(openSshPrivateKey);
        var publicKey = privateKey.GeneratePublicKey();
        var signer = new Ed25519Signer();
        signer.Init(forSigning: true, privateKey);
        signer.BlockUpdate(Handshake, 0, Handshake.Length);
        var signature = signer.GenerateSignature();
        var verifier = new Ed25519Signer();
        verifier.Init(forSigning: false, publicKey);
        verifier.BlockUpdate(Handshake, 0, Handshake.Length);
        if (!verifier.VerifySignature(signature))
        {
            throw new InvalidDataException("签名密钥无法完成自检，请重新导出未加密的 Ed25519 私钥。");
        }
    }

    private static Ed25519PrivateKeyParameters ParsePrivateKey(string openSshPrivateKey)
    {
        using var textReader = new StringReader(openSshPrivateKey);
        using var pemReader = new PemReader(textReader);
        PemObject? pem;
        try
        {
            pem = pemReader.ReadPemObject();
        }
        catch (Exception exception)
        {
            throw new InvalidDataException("签名密钥不是有效的 PEM 文本。", exception);
        }

        if (pem is null ||
            !string.Equals(pem.Type, "OPENSSH PRIVATE KEY", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException(
                "签名密钥必须是 OpenSSH 私钥（文件以 BEGIN OPENSSH PRIVATE KEY 开头）。");
        }

        var keyBlob = pem.Content;
        try
        {
            Ed25519PrivateKeyParameters ed25519Key;
            try
            {
                var key = OpenSshPrivateKeyUtilities.ParsePrivateKeyBlob(keyBlob);
                if (key is not Ed25519PrivateKeyParameters parsed)
                {
                    throw new InvalidDataException("签名密钥必须使用 Ed25519 算法。");
                }

                ed25519Key = parsed;
            }
            catch (InvalidDataException)
            {
                throw;
            }
            catch (Exception exception)
            {
                throw new InvalidDataException(
                    "签名密钥无法解析。请使用未加密的 Ed25519 私钥（ssh-keygen -t ed25519 生成）。",
                    exception);
            }

            return ed25519Key;
        }
        finally
        {
            System.Security.Cryptography.CryptographicOperations.ZeroMemory(keyBlob);
        }
    }
}
